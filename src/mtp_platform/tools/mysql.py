"""MySQL 直连工具（PyMySQL）。

与基线 `mysql_adapter` 的关系：**安全策略整段复用**（白名单 / 生产库拦截 /
写操作同意），传输从 mysql-mcp 换成 PyMySQL 连接。

在这一层实现「禁止连接生产数据库」的**最后一道防线**：

- `host` 必须在 `security.mysql_allow_hosts` 白名单内；
- `database` 命中 `security.mysql_deny_databases` 直接拒绝；
- 写操作需要 `context.allow_write`（由 CLI 的 `--allow-write` 显式放行）；
- 写操作**必须在事务里**执行并核对影响行数，超过 `mysql_max_affected_rows` 直接回滚；
- `update` / `delete` **必须带 where**，否则拒绝（防止误伤全表）；
- **凭据永远不进 summary/证据**，只以 host/database/user 形式出现。
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any, Iterable

from ..contracts.adapters import ActionResult, StepContext
from ..contracts.errors import ConfigError, PolicyDeniedError, ToolExecutionError
from .base import BaseTool

try:  # 未安装驱动时给出明确提示，而不是 ImportError 炸栈
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]
    DictCursor = None  # type: ignore[assignment]

_ACTIONS: dict[str, str] = {
    "health_check": "SELECT 1 探活",
    "databases": "列出数据库",
    "tables": "列出表",
    "describe": "查看表结构",
    "query": "执行 SELECT",
    "fetch": "按条件取行",
    "count": "按条件计数",
    "insert": "插入一行",
    "update": "按条件更新",
    "delete": "按条件删除",
}

_WRITE_ACTIONS = {"insert", "update", "delete"}

# 标识符白名单（表名/列名只允许这种形态，再拼进 SQL —— 值一律走参数绑定）
_IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _quote_ident(name: Any) -> str:
    text = str(name)
    if not _IDENT_RE.match(text):
        raise ConfigError(f"非法的标识符: {text!r}", detail="表名/列名只允许字母数字下划线")
    return f"`{text}`"


class MysqlTool(BaseTool):
    name = "mysql"

    def actions(self) -> dict[str, str]:
        return dict(_ACTIONS)

    # -- 策略（复用基线逻辑）-----------------------------------------------
    def _credentials(self, args: dict[str, Any], context: StepContext) -> dict[str, Any]:
        creds = args.get("credentials")
        if creds is None:
            # 允许用例从 secrets 组装，避免把密码写进 YAML
            creds = context.secrets.get("credentials") if context.secrets else None
        if not isinstance(creds, dict):
            raise ConfigError(
                "mysql 步骤缺少 credentials",
                adapter=self.name,
                detail="需要 {host,user,password,database}；密码请通过环境变量注入",
            )
        if not creds.get("host"):
            raise ConfigError("mysql credentials 缺少 host", adapter=self.name)

        host = str(creds["host"])
        allowed = [str(h) for h in (self.config.security.get("mysql_allow_hosts") or [])]
        if not any(fnmatch.fnmatch(host, pattern) for pattern in allowed):
            raise PolicyDeniedError(
                f"MySQL 目标不在白名单: {host}",
                adapter=self.name,
                detail=f"允许: {', '.join(allowed) or '(空)'}",
            )

        database = creds.get("database")
        self._assert_database_allowed(str(database) if database else None)
        return dict(creds)

    def _assert_database_allowed(self, database: str | None) -> None:
        if not database:
            return
        deny = [str(d).lower() for d in (self.config.security.get("mysql_deny_databases") or [])]
        low = database.lower()
        for pattern in deny:
            if low == pattern or fnmatch.fnmatch(low, pattern):
                raise PolicyDeniedError(
                    f"禁止连接数据库: {database}",
                    adapter=self.name,
                    detail="生产库被 security.mysql_deny_databases 拦截",
                )

    def _assert_write_allowed(self, action: str, args: dict[str, Any], context: StepContext) -> None:
        is_write = action in _WRITE_ACTIONS or (
            action == "query" and args.get("read_only") is False
        )
        if not is_write:
            return
        require = bool(self.config.security.get("mysql_require_write_consent", True))
        if require and not context.allow_write:
            raise PolicyDeniedError(
                f"写操作被拒绝: {action}",
                adapter=self.name,
                action=action,
                detail="需要 CLI 显式传 --allow-write（用例不能自行提权）",
            )

    def pre_execute(self, action: str, args: dict[str, Any], context: StepContext) -> None:
        """策略闸门：白名单 + 生产库拦截 + 写操作同意。"""
        args = args or {}
        self._credentials(args, context)
        self._assert_write_allowed(action, args, context)

    # -- 连接 ---------------------------------------------------------------
    def _connect(self, credentials: dict[str, Any]):
        if pymysql is None:  # pragma: no cover - 依赖缺失路径
            raise ConfigError(
                "未安装 PyMySQL，无法使用 mysql 工具",
                adapter=self.name,
                detail="安装： uv sync --extra mysql",
            )
        timeout = float(credentials.get("connect_timeout", 10))
        return pymysql.connect(
            host=str(credentials["host"]),
            port=int(credentials.get("port", 3306)),
            user=credentials.get("user"),
            password=credentials.get("password"),
            database=credentials.get("database") or None,
            connect_timeout=timeout,
            read_timeout=float(credentials.get("read_timeout", 30)),
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
        )

    # -- 执行 ---------------------------------------------------------------
    def do_execute(self, action: str, args: dict[str, Any], context: StepContext) -> ActionResult:
        args = args or {}
        credentials = self._credentials(args, context)
        started = time.monotonic()
        conn = self._connect(credentials)
        try:
            if action in _WRITE_ACTIONS:
                outcome = self._write(conn, action, args)
            else:
                outcome = self._read(conn, action, args)
        finally:
            conn.close()

        elapsed_ms = int((time.monotonic() - started) * 1000)
        data: dict[str, Any] = {
            "duration_ms": elapsed_ms,
            "host": credentials.get("host"),
            "database": credentials.get("database"),
        }
        data.update(outcome)

        return ActionResult.success(
            action,
            adapter=self.name,
            data=data,
            summary=_summarize(action, data, credentials, elapsed_ms),
        )

    # -- 读 -----------------------------------------------------------------
    def _read(self, conn: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 200))
        with conn.cursor() as cur:
            if action == "health_check":
                cur.execute("SELECT 1 AS ok")
                return {"ok": True}

            if action == "databases":
                cur.execute("SHOW DATABASES")
                rows = cur.fetchall()
                return {"databases": [next(iter(r.values())) for r in rows]}

            if action == "tables":
                cur.execute("SHOW TABLES")
                rows = cur.fetchall()
                return {"tables": [next(iter(r.values())) for r in rows]}

            if action == "describe":
                table = _quote_ident(args.get("table_name"))
                cur.execute(f"DESCRIBE {table}")
                return {"columns": cur.fetchall()}

            if action == "count":
                table = _quote_ident(args.get("table_name"))
                where, params = _where_clause(args)
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}{where}", params)
                row = cur.fetchone() or {}
                return {"count": int(row.get("n", 0))}

            if action == "fetch":
                table = _quote_ident(args.get("table_name"))
                where, params = _where_clause(args)
                order = f" ORDER BY {_quote_ident(args['order_by'])}" if args.get("order_by") else ""
                cur.execute(f"SELECT * FROM {table}{where}{order} LIMIT %s", [*params, limit])
                rows = cur.fetchall()
                return {"rows": rows, "row_count": len(rows)}

            # query：只读 SQL。写语句必须走 insert/update/delete（那三个才有同意与行数闸门）
            sql = str(args.get("sql") or "").strip()
            if not sql:
                raise ConfigError("mysql query 缺少 sql", adapter=self.name)
            if args.get("read_only") is False:
                raise PolicyDeniedError(
                    "query 不支持写模式，请改用 insert/update/delete",
                    adapter=self.name,
                    detail="写语句必须走专用 action，才能套用同意与影响行数上限",
                )
            cur.execute(sql, args.get("where_params") or None)
            rows = cur.fetchall()
            return {"rows": rows, "row_count": len(rows)}

    # -- 写（事务内核对影响行数）-------------------------------------------
    def _write(self, conn: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
        cap = int(self.config.security.get("mysql_max_affected_rows", 1000))
        try:
            with conn.cursor() as cur:
                if action == "insert":
                    table = _quote_ident(args.get("table_name"))
                    row = args.get("row") or {}
                    if not isinstance(row, dict) or not row:
                        raise ConfigError("mysql insert 需要非空 row", adapter=self.name)
                    cols = ", ".join(_quote_ident(k) for k in row)
                    marks = ", ".join(["%s"] * len(row))
                    cur.execute(
                        f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values())
                    )
                    affected = cur.rowcount
                else:
                    table = _quote_ident(args.get("table_name"))
                    where, params = _where_clause(args, required=True)
                    if action == "update":
                        updates = args.get("updates") or {}
                        if not isinstance(updates, dict) or not updates:
                            raise ConfigError("mysql update 需要非空 updates", adapter=self.name)
                        sets = ", ".join(f"{_quote_ident(k)} = %s" for k in updates)
                        cur.execute(
                            f"UPDATE {table} SET {sets}{where}",
                            [*updates.values(), *params],
                        )
                    else:  # delete
                        cur.execute(f"DELETE FROM {table}{where}", params)
                    affected = cur.rowcount

                if affected > cap:
                    conn.rollback()
                    raise PolicyDeniedError(
                        f"{action} 将影响 {affected} 行，超过上限 {cap}",
                        adapter=self.name,
                        action=action,
                        detail="已回滚。确需批量修改请提高 security.mysql_max_affected_rows",
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {"rows_affected": affected}

    def health(self) -> bool:
        """驱动可用即算健康（不主动建连）。"""
        return pymysql is not None


def _where_clause(args: dict[str, Any], *, required: bool = False) -> tuple[str, list[Any]]:
    """构造 WHERE 子句。`where` 是 SQL 片段（列名部分由调用方保证），值走 `where_params`。"""
    where = str(args.get("where") or "").strip()
    if not where:
        if required:
            raise PolicyDeniedError(
                "缺少 where 条件，拒绝执行",
                detail="update/delete 必须带 where，防止误伤全表",
            )
        return "", []
    params = list(args.get("where_params") or [])
    return f" WHERE {where}", params


def _summarize(
    action: str, data: dict[str, Any], credentials: dict[str, Any], elapsed_ms: int
) -> str:
    """只出现 host/database/行数，绝不含密码。"""
    where = f"{credentials.get('host')}/{credentials.get('database')}"
    if action in {"query", "fetch"}:
        rows = data.get("row_count", len(data.get("rows", []) or []))
        return f"{where} {action} 返回 {rows} 行（{elapsed_ms}ms）"
    if action in _WRITE_ACTIONS:
        return f"{where} {action} 影响 {data.get('rows_affected', '?')} 行（{elapsed_ms}ms）"
    return f"{where} {action} 完成（{elapsed_ms}ms）"


__all__: Iterable[str] = ["MysqlTool"]
