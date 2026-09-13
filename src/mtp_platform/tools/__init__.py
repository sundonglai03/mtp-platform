"""直连工具层（执行链路**不走 MCP**）。

每个工具实现 `execute(action, args, context) -> ActionResult`；
`ToolRegistry` 负责装配与生命周期，engine 只依赖 `engine.ports.ToolRegistry` 协议。
"""

from mtp_platform.tools.base import ActionResult, BaseTool, StepContext
from mtp_platform.tools.http import ApiTool
from mtp_platform.tools.registry import ToolRegistry

__all__ = ["BaseTool", "ApiTool", "ToolRegistry", "ActionResult", "StepContext"]
