"""执行引擎：编排、断言、证据、数据台账。

只依赖内联的 `mtp_platform.contracts`（契约）与 `ports.ToolRegistry`（端口）。
具体工具实现由 platform 适配层注入，engine 不感知。
"""

from mtp_platform.engine.assertions import AssertionEngine, AssertionResult
from mtp_platform.engine.data_manager import DataManager
from mtp_platform.engine.evidence import EvidenceStore
from mtp_platform.engine.orchestrator import TestRunner
from mtp_platform.engine.ports import ToolRegistry

__all__ = [
    "TestRunner",
    "AssertionEngine",
    "AssertionResult",
    "EvidenceStore",
    "DataManager",
    "ToolRegistry",
]
