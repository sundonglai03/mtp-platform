"""Fixture 的格式化与静态检查。

执行与同一任务内的 cleanup 都由编排器完成；这里不写入任何独立任务状态文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FixtureSpec:
    fixture_id: str
    setup_action: str
    setup_args: dict[str, Any] = field(default_factory=dict)
    verify: list[dict[str, Any]] = field(default_factory=list)
    cleanup_action: str | None = None
    cleanup_args: dict[str, Any] = field(default_factory=dict)
    cleanup_required: bool = False
    description: str = ""

    def to_cleanup_step(self) -> dict[str, Any] | None:
        if not self.cleanup_action:
            return None
        return {
            "id": f"cleanup:{self.fixture_id}",
            "action": self.cleanup_action,
            "args": dict(self.cleanup_args),
            "on_failure": "continue",
        }


class FixturePlanner:
    def plan(self, case: dict[str, Any]) -> list[FixtureSpec]:
        specs: list[FixtureSpec] = []
        for raw in case.get("fixtures") or []:
            cleanup = raw.get("cleanup") or {}
            specs.append(
                FixtureSpec(
                    fixture_id=str(raw.get("id")),
                    setup_action=str(raw.get("action")),
                    setup_args=dict(raw.get("args") or {}),
                    verify=list(raw.get("verify") or []),
                    cleanup_action=str(cleanup.get("action")) if cleanup.get("action") else None,
                    cleanup_args=dict(cleanup.get("args") or {}),
                    cleanup_required=bool(cleanup.get("required")),
                    description=str(raw.get("description") or ""),
                )
            )
        return specs

    def issues(self, case: dict[str, Any]) -> list[str]:
        """返回不阻断执行的 fixture 风险提示。"""
        problems: list[str] = []
        for spec in self.plan(case):
            if not spec.setup_action.startswith(("mysql.", "api.", "ssh.")):
                problems.append(
                    f"fixture {spec.fixture_id}: 动作 {spec.setup_action} 通常不是数据准备动作"
                )
            if spec.setup_action in {"mysql.insert", "mysql.update", "mysql.delete"}:
                if not spec.cleanup_action:
                    problems.append(
                        f"fixture {spec.fixture_id}: 写入了数据但没有 cleanup，可能污染测试库"
                    )
                if not _mentions_run_id(spec.setup_args):
                    problems.append(
                        f"fixture {spec.fixture_id}: 写入数据未使用 {{{{ run.id }}}}，并行任务可能互相干扰"
                    )
        return problems


def _mentions_run_id(value: Any) -> bool:
    if isinstance(value, str):
        return "run.id" in value
    if isinstance(value, dict):
        return any(_mentions_run_id(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_mentions_run_id(item) for item in value)
    return False
