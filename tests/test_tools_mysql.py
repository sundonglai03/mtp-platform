"""MySQL 直连工具测试（不连真实数据库）。

覆盖基线 mysql_adapter 的验收点：白名单、生产库拦截、写同意、影响行数上限；
外加直连实现新增的「update/delete 必须带 where」「超限回滚」。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from mtp_platform.contracts.adapters import StepContext
from mtp_platform.contracts.config import PlatformConfig
from mtp_platform.tools import mysql as mysql_module
from mtp_platform.tools.mysql import MysqlTool

CREDS = {"host": "127.0.0.1", "user": "u", "password": "s3cret", "database": "testdb"}


@pytest.fixture
def fake_mysql(monkeypatch):
    """假的 pymysql：记录 SQL、可配置返回行数与受影响行数。"""
    state = types.SimpleNamespace(
        rows=[], one=None, rowcount=0, executed=[], connections=[], raise_on_execute=None
    )

    class _Cursor:
        def __init__(self):
            self.rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            state.executed.append((sql, params))
            if state.raise_on_execute:
                raise state.raise_on_execute
            self.rowcount = state.rowcount
            return state.rowcount

        def fetchall(self):
            return list(state.rows)

        def fetchone(self):
            return state.one

    class _Conn:
        def __init__(self):
            self.committed = False
            self.rolled_back = False
            self.closed = False
            state.connections.append(self)

        def cursor(self):
            return _Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    module = types.SimpleNamespace(
        connect=lambda **kw: _Conn(), cursors=types.SimpleNamespace(DictCursor=object)
    )
    monkeypatch.setattr(mysql_module, "pymysql", module)
    return state


def _ctx(**kw) -> StepContext:
    return StepContext(**kw)


def _cfg(**security) -> PlatformConfig:
    security.setdefault("mysql_allow_hosts", ["127.0.0.1", "localhost"])
    security.setdefault("mysql_deny_databases", ["gkpt", "prod"])
    return PlatformConfig(raw={"security": security}, path=Path("mtp_config.yaml"))


# --- 策略 ------------------------------------------------------------------
def test_missing_credentials_is_config_error(config):
    result = MysqlTool(config).execute("query", {"sql": "SELECT 1"}, _ctx())
    assert result.ok is False
    assert result.error.code == "config_error"


def test_host_not_in_whitelist_is_denied(config):
    creds = {**CREDS, "host": "8.8.8.8"}
    result = MysqlTool(config).execute("query", {"credentials": creds, "sql": "SELECT 1"}, _ctx())
    assert result.ok is False
    assert result.error.code == "policy_denied"


def test_production_database_is_denied():
    tool = MysqlTool(_cfg())
    creds = {**CREDS, "database": "gkpt"}
    result = tool.execute("query", {"credentials": creds, "sql": "SELECT 1"}, _ctx())
    assert result.ok is False
    assert result.error.code == "policy_denied"


def test_write_requires_allow_write(config, fake_mysql):
    result = MysqlTool(config).execute(
        "insert",
        {"credentials": CREDS, "table_name": "t", "row": {"a": 1}},
        _ctx(allow_write=False),
    )
    assert result.ok is False
    assert result.error.code == "policy_denied"


def test_identifier_injection_is_rejected(config, fake_mysql):
    result = MysqlTool(config).execute(
        "insert",
        {"credentials": CREDS, "table_name": "t; DROP TABLE x", "row": {"a": 1}},
        _ctx(allow_write=True),
    )
    assert result.ok is False
    assert result.error.code == "config_error"


def test_update_without_where_is_denied(config, fake_mysql):
    result = MysqlTool(config).execute(
        "update",
        {"credentials": CREDS, "table_name": "t", "updates": {"a": 1}},
        _ctx(allow_write=True),
    )
    assert result.ok is False
    assert result.error.code == "policy_denied"


# --- 读 --------------------------------------------------------------------
def test_query_returns_rows(config, fake_mysql):
    fake_mysql.rows = [{"id": 1}, {"id": 2}]
    result = MysqlTool(config).execute(
        "query", {"credentials": CREDS, "sql": "SELECT id FROM t"}, _ctx()
    )
    assert result.ok, result.error
    assert result.data["row_count"] == 2
    assert result.data["rows"] == [{"id": 1}, {"id": 2}]


def test_fetch_binds_where_params_not_string_interpolation(config, fake_mysql):
    fake_mysql.rows = [{"id": 7}]
    result = MysqlTool(config).execute(
        "fetch",
        {
            "credentials": CREDS,
            "table_name": "users",
            "where": "name = %s",
            "where_params": ["bob"],
            "limit": 5,
        },
        _ctx(),
    )
    assert result.ok, result.error
    sql, params = fake_mysql.executed[-1]
    assert "WHERE name = %s" in sql
    assert params == ["bob", 5]  # 值走参数绑定，不拼进 SQL


def test_count_uses_parameterized_where(config, fake_mysql):
    fake_mysql.one = {"n": 3}
    result = MysqlTool(config).execute(
        "count",
        {"credentials": CREDS, "table_name": "t", "where": "a = %s", "where_params": [1]},
        _ctx(),
    )
    assert result.ok, result.error
    assert result.data["count"] == 3


# --- 写 --------------------------------------------------------------------
def test_insert_commits_and_reports_affected(config, fake_mysql):
    fake_mysql.rowcount = 1
    result = MysqlTool(config).execute(
        "insert",
        {"credentials": CREDS, "table_name": "t", "row": {"a": 1, "b": 2}},
        _ctx(allow_write=True),
    )
    assert result.ok, result.error
    assert result.data["rows_affected"] == 1
    conn = fake_mysql.connections[-1]
    assert conn.committed is True
    assert conn.closed is True


def test_write_over_cap_rolls_back(config, fake_mysql):
    fake_mysql.rowcount = 9999  # 上限默认 1000
    result = MysqlTool(config).execute(
        "delete",
        {"credentials": CREDS, "table_name": "t", "where": "a = %s", "where_params": [1]},
        _ctx(allow_write=True),
    )
    assert result.ok is False
    assert result.error.code == "policy_denied"
    conn = fake_mysql.connections[-1]
    assert conn.rolled_back is True
    assert conn.committed is False


def test_error_during_write_rolls_back(config, fake_mysql):
    fake_mysql.raise_on_execute = RuntimeError("boom")
    result = MysqlTool(config).execute(
        "update",
        {
            "credentials": CREDS,
            "table_name": "t",
            "updates": {"a": 1},
            "where": "id = %s",
            "where_params": [5],
        },
        _ctx(allow_write=True),
    )
    assert result.ok is False
    assert fake_mysql.connections[-1].rolled_back is True


# --- 脱敏 / 依赖 -----------------------------------------------------------
def test_summary_never_contains_password(config, fake_mysql):
    fake_mysql.rows = [{"id": 1}]
    result = MysqlTool(config).execute(
        "query", {"credentials": CREDS, "sql": "SELECT id FROM t"}, _ctx()
    )
    assert "s3cret" not in result.summary
    assert "s3cret" not in str(result.data)


def test_missing_pymysql_gives_clear_error(config, monkeypatch):
    monkeypatch.setattr(mysql_module, "pymysql", None)
    result = MysqlTool(config).execute(
        "query", {"credentials": CREDS, "sql": "SELECT 1"}, _ctx()
    )
    assert result.ok is False
    assert result.error.code == "config_error"
    assert "PyMySQL" in result.error.message
