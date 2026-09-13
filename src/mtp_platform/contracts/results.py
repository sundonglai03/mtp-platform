"""运行状态与结果模型。

状态机（orchestrator/TASK）::

    queued ──▶ running ──┬─▶ passed
                         ├─▶ failed     步骤/断言失败
                         ├─▶ error      平台自身出错（配置、MCP 不可用…）
                         └─▶ cancelled  被主动取消

所有时间戳用 UTC ISO8601 字符串，耗时用毫秒整数 —— 报告和趋势都直接消费这两个字段。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class RunState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {RunState.PASSED, RunState.FAILED, RunState.ERROR, RunState.CANCELLED}


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    """`20260912T201530-4f3c9a`：可读前缀 + 随机后缀，天然按时间排序。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass
class StepResult:
    step_id: str
    action: str
    phase: str = "steps"
    index: int = 0
    status: StepStatus = StepStatus.PENDING
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    attempts: int = 0
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == StepStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "phase": self.phase,
            "index": self.index,
            "action": self.action,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "summary": self.summary,
            "data": self.data,
            "error": self.error,
            "evidence": self.evidence,
        }


@dataclass
class CaseResult:
    run_id: str
    case_id: str
    title: str = ""
    module: str = ""
    priority: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""
    status: RunState = RunState.QUEUED
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    steps: list[StepResult] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    cleanup: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    # -- 派生统计 -----------------------------------------------------------
    @property
    def passed(self) -> bool:
        return self.status == RunState.PASSED

    def counts(self) -> dict[str, int]:
        total = len(self.assertions)
        failed = sum(1 for a in self.assertions if not a.get("passed"))
        return {
            "assertions_total": total,
            "assertions_passed": total - failed,
            "assertions_failed": failed,
        }

    def first_failure(self) -> dict[str, Any] | None:
        """定位到最早的失败点：优先失败步骤，其次失败断言。"""
        for step in self.steps:
            if step.status in {StepStatus.FAILED, StepStatus.ERROR}:
                return {"kind": "step", **step.to_dict()}
        for assertion in self.assertions:
            if not assertion.get("passed"):
                return {"kind": "assertion", **assertion}
        if self.error:
            return {"kind": "case", **self.error}
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "title": self.title,
            "module": self.module,
            "priority": self.priority,
            "tags": self.tags,
            "source": self.source,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "counts": self.counts(),
            "steps": [s.to_dict() for s in self.steps],
            "assertions": self.assertions,
            "evidence": self.evidence,
            "cleanup": self.cleanup,
            "error": self.error,
            "warnings": self.warnings,
        }
