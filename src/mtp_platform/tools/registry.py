"""工具注册表：装配直连工具，并实现 engine 的 `ports.ToolRegistry` 协议。"""

from __future__ import annotations

from typing import Any, Callable

from mtp_contracts.config import PlatformConfig
from mtp_contracts.errors import ConfigError
from .base import BaseTool
from .http import ApiTool
from .mysql import MysqlTool
from .playwright import PlaywrightTool
from .ssh import SshTool


def _default_builders() -> dict[str, Callable[[PlatformConfig], BaseTool]]:
    """内置工具工厂。新增直连工具只需在这里挂一项。"""
    return {
        "api": ApiTool,
        "ssh": SshTool,
        "mysql": MysqlTool,
        "playwright": PlaywrightTool,
    }


class ToolRegistry:
    def __init__(self, config: PlatformConfig) -> None:
        self.config = config
        self._instances: dict[str, BaseTool] = {}
        self._overrides: dict[str, BaseTool] = {}

    # -- 注册 -------------------------------------------------------------
    def register(self, name: str, tool: BaseTool) -> None:
        """注入 mock / 自定义实现（测试与二次开发用）。"""
        self._overrides[name] = tool

    def available(self) -> list[str]:
        return sorted(set(_default_builders()) | set(self._overrides))

    def is_active(self, name: str) -> bool:
        """该工具是否已经实例化（常用于判断浏览器会话是否已经起过）。"""
        return name in self._instances or name in self._overrides

    def active_names(self) -> list[str]:
        """已经实例化的工具名（引擎据此决定要给谁做用例级会话隔离）。"""
        return sorted(set(self._instances) | set(self._overrides))

    # -- 获取 -------------------------------------------------------------
    def get(self, name: str) -> BaseTool:
        if name in self._overrides:
            return self._overrides[name]
        if name in self._instances:
            return self._instances[name]

        builders = _default_builders()
        if name not in builders:
            raise ConfigError(
                f"未知工具: {name}",
                detail=f"可用: {', '.join(sorted(builders))}（或通过 register() 注入）",
            )

        instance = builders[name](self.config)
        self._instances[name] = instance
        return instance

    def close_all(self) -> None:
        """释放所有工具持有的连接/进程。

        实例与 `register()` 注入的覆盖实现都要关：engine 调 `close_all()` 时
        期望资源确实被释放，不能只关内部实例。
        """
        for tool in list(self._instances.values()) + list(self._overrides.values()):
            try:
                tool.close()
            except Exception:  # noqa: BLE001
                pass
        self._instances.clear()

    def __enter__(self) -> "ToolRegistry":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close_all()
