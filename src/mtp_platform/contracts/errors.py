"""统一错误模型。

适配器、编排器、断言、报告都只抛/接这一套异常，保证：
- 能区分**超时 / 认证失败 / 工具失败 / 网络错误 / 配置错误 / 策略拒绝**；
- `code` 是稳定的机器可读标识，`message` 是给人看的（已脱敏）。

注意：消息里**不允许**出现凭据原文，脱敏在 `redaction.redact` 中完成。
"""

from __future__ import annotations

from typing import Any


class MtpError(Exception):
    """所有平台错误的基类。"""

    code = "mtp_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        adapter: str | None = None,
        action: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.adapter = adapter
        self.action = action
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            data["detail"] = self.detail
        if self.adapter:
            data["adapter"] = self.adapter
        if self.action:
            data["action"] = self.action
        if self.extra:
            data["extra"] = self.extra
        return data

    def __str__(self) -> str:  # pragma: no cover - 展示用
        parts = [f"[{self.code}] {self.message}"]
        if self.adapter or self.action:
            parts.append(f"(adapter={self.adapter} action={self.action})")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)


class TimeoutError_(MtpError):  # noqa: N801 - 尾部下划线避免遮蔽内建 TimeoutError
    """步骤或调用超时。可重试。"""

    code = "timeout"
    retryable = True


class AuthenticationError(MtpError):
    """认证失败：密码错、密钥错、Token 过期。不可重试。"""

    code = "auth_failed"


class ToolExecutionError(MtpError):
    """工具本身执行失败：命令非零退出、SQL 语法错、断言前置条件不满足。"""

    code = "tool_failed"


class NetworkError(MtpError):
    """网络层错误：连不上、DNS 失败、连接被重置。可重试。"""

    code = "network_error"
    retryable = True


class ConfigError(MtpError):
    """配置缺失或非法：MCP 未配置、参数形状不对。"""

    code = "config_error"


class PolicyDeniedError(MtpError):
    """被安全策略拒绝：目标不在白名单、命中生产库、超出影响行数。"""

    code = "policy_denied"


class McpUnavailableError(MtpError):
    """MCP 服务不可用：进程起不来、握手失败、工具名不存在。"""

    code = "mcp_unavailable"


class CaseValidationError(MtpError):
    """用例不合法，不允许进入执行器。"""

    code = "case_invalid"


class CancelledError_(MtpError):  # noqa: N801
    """任务被主动取消。"""

    code = "cancelled"


class StateCorruptError(MtpError):
    """平台自己写的状态文件损坏（目前是 fixture 清理台账 ledger）。

    刻意**不**归到 `config_error`：配置是人工维护的，状态文件是平台自己写的，
    两者的排查路径完全不同 —— 前者去改配置，后者要去查「上次是不是中途被打断了」。
    也刻意不做成静默降级：台账丢了 = 中断任务的补清理能力丢了，
    这种问题必须浮出来，不能被当成「没有待清理项」。
    """

    code = "state_corrupt"


# 供适配器把任意底层异常归类成上述稳定类型
_AUTH_HINTS = (
    "auth",
    "access denied",
    "permission denied",
    "invalid password",
    "authentication",
    "login failed",
    "401",
    "403",
)
_NETWORK_HINTS = (
    "connection refused",
    "connection reset",
    "no route to host",
    "name or service not known",
    "nodename nor servname",
    "timed out connecting",
    "unreachable",
    "socket",
    "broken pipe",
    "eof occurred",
)
_TIMEOUT_HINTS = ("timeout", "timed out", "deadline exceeded")


def classify_exception(
    exc: BaseException,
    *,
    adapter: str | None = None,
    action: str | None = None,
) -> MtpError:
    """把底层第三方异常归类成平台错误类型。

    仅做关键字归类，不吞噬原始文本（原始文本放在 `detail`，调用方负责脱敏）。
    """
    if isinstance(exc, MtpError):
        if adapter and not exc.adapter:
            exc.adapter = adapter
        if action and not exc.action:
            exc.action = action
        return exc

    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()

    if isinstance(exc, (TimeoutError,)) or any(h in low for h in _TIMEOUT_HINTS):
        cls = TimeoutError_
    elif any(h in low for h in _AUTH_HINTS):
        cls = AuthenticationError
    elif any(h in low for h in _NETWORK_HINTS):
        cls = NetworkError
    else:
        cls = ToolExecutionError

    return cls(
        text.splitlines()[0][:500] if text else "unknown error",
        detail=text[:4000],
        adapter=adapter,
        action=action,
    )
