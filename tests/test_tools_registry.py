"""工具注册表：装配、注入、生命周期。"""

from __future__ import annotations

import pytest

from mtp_contracts.adapters import ActionResult
from mtp_contracts.errors import ConfigError
from mtp_platform.tools.registry import ToolRegistry


class _StubTool:
    name = "stub"

    def __init__(self) -> None:
        self.closed = False

    def actions(self) -> dict[str, str]:
        return {"do": "do"}

    def execute(self, action, args, context):
        return ActionResult.success(action, adapter=self.name)

    def close(self) -> None:
        self.closed = True


def test_builtin_api_tool_is_available(config):
    registry = ToolRegistry(config)
    assert "api" in registry.available()
    assert registry.get("api").name == "api"
    registry.close_all()


def test_unknown_tool_raises(config):
    with pytest.raises(ConfigError):
        ToolRegistry(config).get("nope")


def test_register_overrides_and_lifecycle(config):
    registry = ToolRegistry(config)
    stub = _StubTool()

    assert registry.is_active("stub") is False
    registry.register("stub", stub)
    assert registry.is_active("stub") is True
    assert registry.get("stub") is stub

    registry.close_all()
    assert stub.closed is True
