"""引擎对外部能力的**端口**（依赖倒置）。

`engine` 不依赖任何具体工具实现（既不认识 MCP，也不认识 direct tools），
只依赖这里的 `Protocol`；具体实现与测试用 fake 由 platform 注入。

这样 engine 可以在**没有任何外部服务**的情况下用 fake 注册表跑通完整用例。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mtp_contracts.adapters import Adapter


@runtime_checkable
class ToolRegistry(Protocol):
    """工具注册表。engine 只用到这三个方法（其余方法属于 platform 的体检/装配）。"""

    def get(self, name: str) -> Adapter:
        """取一个工具；不存在时抛错。"""
        ...

    def is_active(self, name: str) -> bool:
        """该工具是否已经注册或实例化。"""
        ...

    def close_all(self) -> None:
        """释放所有工具持有的连接/进程。"""
        ...
