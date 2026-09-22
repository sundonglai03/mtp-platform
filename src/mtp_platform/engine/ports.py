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

    def active_names(self) -> list[str]:
        """已经实例化的工具名。

        **可选能力**：引擎用它找出「有会话状态的工具」，在用例之间做会话隔离
        （只对声明了 `reset_session` 的工具生效）。引擎用 `getattr` 探测，
        未实现时退化为「不隔离」，因此老注册表与测试替身不必实现它。
        """
        ...

    def close_all(self) -> None:
        """释放所有工具持有的连接/进程。"""
        ...
