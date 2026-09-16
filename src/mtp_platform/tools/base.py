"""直连工具的基类与公共逻辑。

所有工具实现同一接口：`execute(action, args, context) -> ActionResult`。
与 MCP 版适配器的区别是**没有任何 MCP 客户端 / 子进程**，直接调用底层库
（requests / paramiko / 数据库连接器 / playwright …）。

`ActionResult` / `StepContext` 复用契约层模型，因此引擎侧零改动。
"""

from __future__ import annotations

from typing import Any

from mtp_contracts.adapters import ActionResult, StepContext
from mtp_contracts.config import PlatformConfig
from mtp_contracts.errors import ConfigError, MtpError, classify_exception

__all__ = ["ActionResult", "StepContext", "BaseTool"]


class BaseTool:
    """工具公共部分：action 白名单、策略闸门、异常兜底。"""

    name = "base"

    def __init__(self, config: PlatformConfig) -> None:
        self.config = config

    def actions(self) -> dict[str, str]:
        """语义 action -> 说明（或底层方法标识）。"""
        return {}

    def pre_execute(self, action: str, args: dict[str, Any], context: StepContext) -> None:
        """策略闸门。只对**用例声明的步骤**生效；平台内部可信调用走 `do_execute`。"""

    def do_execute(
        self, action: str, args: dict[str, Any], context: StepContext
    ) -> ActionResult:
        raise NotImplementedError

    def execute(
        self, action: str, args: dict[str, Any], context: StepContext
    ) -> ActionResult:
        if action not in self.actions():
            return ActionResult.failure(
                action,
                ConfigError(
                    f"{self.name} 不支持 action: {action}",
                    adapter=self.name,
                    action=action,
                    detail=f"可用: {', '.join(sorted(self.actions()))}",
                ),
                adapter=self.name,
            )
        try:
            self.pre_execute(action, args, context)
            return self.do_execute(action, args, context)
        except MtpError as exc:
            exc.adapter = exc.adapter or self.name
            exc.action = exc.action or action
            return ActionResult.failure(action, exc, adapter=self.name)
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让工具异常穿透
            return ActionResult.failure(
                action,
                classify_exception(exc, adapter=self.name, action=action),
                adapter=self.name,
            )

    def health(self) -> bool:
        return True

    def close(self) -> None:
        return None
