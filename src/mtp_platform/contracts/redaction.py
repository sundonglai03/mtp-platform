"""脱敏。

两类泄露渠道都要堵住：

1. **按字段名**：`{"password": "x"}`、`Cookie: ...`、`Authorization: ...`
   → 按 key 递归替换。
2. **按值**：已知的秘密**值**出现在自由文本里（stdout、响应体、命令行）
   → 注册到 `SecretRegistry`，输出前按值替换。

设计约束（对应各 TASK 的验收标准）：
- 报告/证据/日志里不出现凭据原文；
- 断言结果里的 `actual` 若来自敏感源，走同一套脱敏。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

DEFAULT_REDACT_KEYS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "set-cookie",
    "authorization",
    "secret",
    "api_key",
    "apikey",
    "private_key",
    "session",
    "credential",
    "credentials",
)

DEFAULT_PLACEHOLDER = "***REDACTED***"

# 形如 `Authorization: Bearer xxxxx` / `password=xxxx` 的行内模式
_INLINE_PATTERNS = (
    re.compile(r"(?i)\b(authorization|proxy-authorization)\s*:\s*[^\r\n]+"),
    re.compile(r"(?i)\b(cookie|set-cookie)\s*:\s*[^\r\n]+"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\s*[=:]\s*(\"[^\"]*\"|'[^']*'|\S+)"),
)


class SecretRegistry:
    """登记已知秘密的**值**，任何输出前调用 `scrub` 抹掉。"""

    def __init__(self, placeholder: str = DEFAULT_PLACEHOLDER) -> None:
        self.placeholder = placeholder
        self._values: set[str] = set()

    def register(self, value: Any) -> None:
        if value is None:
            return
        text = str(value)
        # 太短的值（如 "1"）会把无关文本全替换掉，只登记有意义的长度
        if len(text) >= 4:
            self._values.add(text)

    def register_many(self, values: Iterable[Any]) -> None:
        for v in values:
            self.register(v)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        out = text
        # 长值先替换，避免短值是长值子串时留下残片
        for value in sorted(self._values, key=len, reverse=True):
            if value and value in out:
                out = out.replace(value, self.placeholder)
        for pattern in _INLINE_PATTERNS:
            out = pattern.sub(_inline_replace, out)
        return out

    @property
    def values(self) -> frozenset[str]:
        return frozenset(self._values)


def _inline_replace(match: re.Match[str]) -> str:
    return f"{match.group(1)}: {DEFAULT_PLACEHOLDER}"


def is_sensitive_key(key: Any, keys: Iterable[str] = DEFAULT_REDACT_KEYS) -> bool:
    name = str(key).strip().lower().replace("-", "_")
    return any(name == k or name.endswith(f"_{k}") or k in name for k in keys)


def redact(
    obj: Any,
    *,
    keys: Iterable[str] = DEFAULT_REDACT_KEYS,
    placeholder: str = DEFAULT_PLACEHOLDER,
    registry: SecretRegistry | None = None,
    _depth: int = 0,
) -> Any:
    """递归脱敏：命中敏感字段名的值替换为 placeholder，其余文本走值替换。

    返回新对象，不修改入参。
    """
    if _depth > 24:
        return "<max-depth>"

    if isinstance(obj, Mapping):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if is_sensitive_key(k, keys):
                out[k] = placeholder if v not in (None, "", [], {}) else v
            else:
                out[k] = redact(
                    v, keys=keys, placeholder=placeholder, registry=registry, _depth=_depth + 1
                )
        return out

    if isinstance(obj, (list, tuple, set)):
        return [
            redact(v, keys=keys, placeholder=placeholder, registry=registry, _depth=_depth + 1)
            for v in obj
        ]

    if isinstance(obj, str):
        text = obj
        if registry is not None:
            text = registry.scrub(text)
        for pattern in _INLINE_PATTERNS:
            text = pattern.sub(_inline_replace, text)
        return text

    return obj


def redact_text(text: str, registry: SecretRegistry | None = None, *, limit: int | None = None) -> str:
    """对自由文本（stdout / 响应体 / SQL）做脱敏。"""
    out = text or ""
    if registry is not None:
        out = registry.scrub(out)
    for pattern in _INLINE_PATTERNS:
        out = pattern.sub(_inline_replace, out)
    if limit is not None and len(out) > limit:
        out = out[:limit] + f"\n...<truncated {len(out) - limit} chars>"
    return out
