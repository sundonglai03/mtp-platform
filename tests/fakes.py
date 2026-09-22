"""测试替身：不依赖任何外部服务的假注册表。

engine 只依赖 `ports.ToolRegistry` 的 `get` / `is_active` / `close_all`；
这里用一个内存实现替代 platform 的真实（direct tools）注册表，
让编排器测试既快又稳、不启任何进程。
"""

from __future__ import annotations

from typing import Any

from mtp_contracts.config import PlatformConfig
from mtp_contracts.errors import ConfigError


class FakeRegistry:
    def __init__(self, config: PlatformConfig | None = None) -> None:
        self.config = config
        self._adapters: dict[str, Any] = {}

    def register(self, name: str, adapter: Any) -> None:
        self._adapters[name] = adapter

    def available(self) -> list[str]:
        return sorted(self._adapters)

    def is_active(self, name: str) -> bool:
        return name in self._adapters

    def active_names(self) -> list[str]:
        return sorted(self._adapters)

    def get(self, name: str) -> Any:
        if name not in self._adapters:
            raise ConfigError(
                f"未知工具: {name}",
                detail=f"可用: {', '.join(self.available()) or '(无)'}",
            )
        return self._adapters[name]

    def close_all(self) -> None:
        for adapter in list(self._adapters.values()):
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
