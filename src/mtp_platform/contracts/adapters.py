"""适配器契约：所有外部工具适配器共享的数据模型与协议。

只含**数据模型 + 协议**（`ActionResult` / `StepContext` / `Adapter`）；
具体实现（`BaseAdapter`、MCP stdio 客户端、各适配器）在 `mtp-adapters`。

有了这一层，`mtp-engine` 可以在**不认识任何具体适配器**的前提下编排执行，
测试时用 fake adapter 即可跑通完整用例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import MtpError


@dataclass
class ActionResult:
    """一次步骤执行的归一化产物。

    - `data`：供变量引用与断言消费的字段（`{{ steps.<id>.<field> }}`）
    - `summary`：可直接进报告的一句话（必须已脱敏）
    - `raw`：原始 content items，交给证据层落盘
    """

    ok: bool
    action: str
    data: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    raw: list[dict[str, Any]] = field(default_factory=list)
    error: MtpError | None = None
    adapter: str = ""

    @classmethod
    def success(
        cls,
        action: str,
        *,
        adapter: str = "",
        data: dict[str, Any] | None = None,
        summary: str = "",
        raw: list[dict[str, Any]] | None = None,
    ) -> "ActionResult":
        return cls(
            ok=True,
            action=action,
            adapter=adapter,
            data=data or {},
            summary=summary,
            raw=raw or [],
        )

    @classmethod
    def failure(cls, action: str, error: MtpError, *, adapter: str = "") -> "ActionResult":
        return cls(
            ok=False,
            action=action,
            adapter=adapter,
            error=error,
            summary=f"{error.code}: {error.message}",
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "adapter": self.adapter,
            "action": self.action,
            "data": self.data,
            "summary": self.summary,
        }
        if self.error is not None:
            out["error"] = self.error.to_dict()
        return out


@dataclass
class StepContext:
    """步骤执行上下文：允许适配器感知运行态（run_id、是否允许写、环境变量）。

    - `env`：用例 `environment` 块（已解析），api 适配器用它拼 base_url；
    - `variables`：用例 `variables`（已解析）；
    - `secrets`：**未解析**的 {逻辑名: 环境变量名}，仅作为兜底；
      正常路径下模板已在编排器里解析完毕。
    """

    run_id: str = ""
    case_id: str = ""
    step_id: str = ""
    allow_write: bool = False
    env: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    def merged_vars(self) -> dict[str, Any]:
        return {**self.env, **self.variables}


class Adapter(Protocol):
    """适配器协议。"""

    name: str

    def execute(self, action: str, args: dict[str, Any], context: StepContext) -> ActionResult:
        ...

    def health(self) -> bool:
        ...

    def close(self) -> None:
        ...
