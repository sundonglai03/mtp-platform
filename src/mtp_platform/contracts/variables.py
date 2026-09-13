"""变量引用解析。

**统一格式**：`{{ 命名空间.路径 }}`，只有这一种写法（不再支持 `$VAR`、`${VAR}`）。

命名空间（固定，越界即报错，避免静默取到空值）：

===== ==========================================
env   `environment` 块里的键，如 `{{ env.base_url }}`
vars  `variables` 块里的用户变量
secrets  `secrets` 块解析后的**值**（日志中会被脱敏）
steps `{{ steps.<step_id>.<字段> }}`，字段来自该步骤的结果
run   `{{ run.id }}` / `{{ run.case_id }}`
now   `{{ now.iso }}` / `{{ now.epoch }}`
===== ==========================================

两种插值语义：
- 整个字符串就是一个引用 → **保留原始类型**（数字还是数字，对象还是对象）；
- 引用嵌在文本里 → 转成字符串拼接。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any, Iterator

TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
# 命名空间.键，允许步骤 id 里的连字符与列表下标，如 steps.seed-user.rows[0].username
PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_-]+|\[\d+\])*$")
_SEGMENT_RE = re.compile(r"\.([A-Za-z0-9_-]+)|\[(\d+)\]")

NAMESPACES = ("env", "vars", "secrets", "steps", "run", "now")

# 各命名空间允许的二级键（steps 除外，它是动态的）
STATIC_KEYS: dict[str, set[str]] = {
    "run": {"id", "case_id"},
    "now": {"iso", "epoch"},
}


class VariableResolutionError(Exception):
    """变量无法解析。`path` 指向出问题的引用，便于报错定位。"""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.path = path


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def iter_references(value: Any) -> Iterator[str]:
    """递归找出所有 `{{ ... }}` 引用表达式（去重前）。"""
    if isinstance(value, str):
        for match in TEMPLATE_RE.finditer(value):
            yield match.group(1).strip()
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from iter_references(k)
            yield from iter_references(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_references(item)


def tokenize(path: str) -> list[str | int]:
    """把 `steps.seed-user.rows[0].username` 切成 ['steps','seed-user','rows',0,'username']。"""
    head, _, remainder = path.partition(".")
    tokens: list[str | int] = [head]
    for match in _SEGMENT_RE.finditer(("." + remainder) if remainder else ""):
        key, index = match.groups()
        tokens.append(key if key is not None else int(index))
    return tokens


def lookup(path: str, context: dict[str, Any]) -> Any:
    """按点分路径取值；任一层缺失返回 MISSING。"""
    node: Any = context
    for part in tokenize(path):
        if isinstance(node, Mapping):
            if part not in node:
                return MISSING
            node = node[part]
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            if not isinstance(part, int) or part >= len(node):
                return MISSING
            node = node[part]
        else:
            return MISSING
    return node


class LazySecrets(Mapping):
    """`secrets` 命名空间的惰性视图。

    `{逻辑名: 环境变量名}`，取值时才去读环境变量 —— 这样「没被用到的 secret
    即使环境变量缺失，也不会阻塞用例」（比如安全策略用例只需要 host，不需要密码）。

    缺环境变量时报明确错误，绝不静默返回空串（否则会跑出一个假失败）。
    """

    def __init__(self, mapping: Mapping[str, str] | None) -> None:
        self._mapping: dict[str, str] = {str(k): str(v) for k, v in (mapping or {}).items()}
        self._cache: dict[str, str] = {}

    def __getitem__(self, key: str) -> str:
        if key in self._cache:
            return self._cache[key]
        if key not in self._mapping:
            raise VariableResolutionError(f"未声明的 secret: {key}")
        env_name = self._mapping[key]
        value = os.environ.get(env_name)
        if value is None:
            raise VariableResolutionError(
                f"缺少环境变量 {env_name}（供 secret {key} 使用）；"
                "凭据只允许通过环境变量注入"
            )
        self._cache[key] = value
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    def __contains__(self, key: object) -> bool:
        return key in self._mapping

    @property
    def env_names(self) -> dict[str, str]:
        return dict(self._mapping)


def resolve_string(
    text: str, context: dict[str, Any], *, path: str = "", _depth: int = 0
) -> Any:
    """解析单个字符串。

    - 恰好是一个引用 → 返回被引用的对象（保留类型）；若它是容器，继续深解析
      （这样 `{{ env.db }}` 能带出里面嵌套的 `{{ secrets.x }}`）
    - 含多个引用或夹杂文本 → 字符串插值
    """
    if _depth > 12:
        raise VariableResolutionError("变量引用嵌套过深（可能是自引用循环）", path=path)

    matches = list(TEMPLATE_RE.finditer(text))
    if not matches:
        return text

    if len(matches) == 1 and matches[0].group(0) == text:
        expr = matches[0].group(1).strip()
        value = lookup(expr, context)
        if value is MISSING:
            raise VariableResolutionError(f"变量未定义: {{{{ {expr} }}}}", path=path)
        if isinstance(value, (Mapping, list, tuple)):
            return resolve(value, context, path=path, _depth=_depth + 1)
        return value

    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        value = lookup(expr, context)
        if value is MISSING:
            raise VariableResolutionError(f"变量未定义: {{{{ {expr} }}}}", path=path)
        return "" if value is None else str(value)

    return TEMPLATE_RE.sub(_sub, text)


def resolve(value: Any, context: dict[str, Any], *, path: str = "", _depth: int = 0) -> Any:
    """深度解析：字典/列表递归，字符串走 `resolve_string`。"""
    if isinstance(value, str):
        return resolve_string(value, context, path=path, _depth=_depth)
    if isinstance(value, Mapping):
        return {
            k: resolve(v, context, path=f"{path}.{k}" if path else str(k), _depth=_depth)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            resolve(v, context, path=f"{path}[{i}]", _depth=_depth) for i, v in enumerate(value)
        ]
    return value
