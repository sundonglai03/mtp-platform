"""编排器测试：状态机、超时、重试、取消，以及「清理必定执行」。

全部走 mock 适配器，不启动真实 MCP，所以又快又稳。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mtp_contracts.adapters import ActionResult, StepContext
from mtp_contracts.errors import PolicyDeniedError, TimeoutError_
from mtp_platform.config import load_config
from fakes import FakeRegistry
from mtp_contracts.results import RunState, StepStatus

ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts" / "_pytest"


class RecordingAdapter:
    """可编程的假适配器：按序返回预置结果，并记录调用。"""

    name = "recorder"

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def _next(self, action: str, args: dict) -> ActionResult:
        self.calls.append((action, dict(args or {})))
        if not self.script:
            return ActionResult.success(action, adapter=self.name, data={}, summary="ok")
        item = self.script.pop(0)
        if callable(item):
            return item(action, args)
        if isinstance(item, ActionResult):
            return item
        if isinstance(item, dict) and item.get("sleep"):
            time.sleep(item["sleep"])
        ok = not (isinstance(item, dict) and item.get("fail"))
        if ok:
            return ActionResult.success(
                action, adapter=self.name, data=dict(item or {}), summary="ok"
            )
        return ActionResult.failure(
            action, PolicyDeniedError("mock failure", adapter=self.name, action=action), adapter=self.name
        )

    def actions(self):
        return {"do": "do", "pause": "pause", "screenshot": "screenshot", "snapshot": "snapshot",
                "console_messages": "console", "network_requests": "network"}

    def pre_execute(self, action, args, context):
        return None

    def do_execute(self, action, args, context):
        return self._next(action, args)

    def execute(self, action, args, context):
        if action not in self.actions():
            return ActionResult.failure(
                action, PolicyDeniedError(f"unknown action {action}", adapter=self.name), adapter=self.name
            )
        self.pre_execute(action, args, context)
        return self.do_execute(action, args, context)

    def close(self):
        self.closed = True


@pytest.fixture()
def config():
    return load_config()


def make_runner(config, adapters: dict, **kw):
    from mtp_platform.engine.orchestrator import TestRunner

    registry = FakeRegistry(config)
    for name, adapter in adapters.items():
        registry.register(name, adapter)
    return TestRunner(config, registry=registry, artifacts_root=ARTIFACTS, **kw)


BASE_CASE = {
    "schema_version": 1,
    "id": "UNIT-001",
    "title": "编排器单元用例",
    "module": "unit",
    "priority": "P1",
    "stoeps": None,
    "steps": [{"id": "s1", "action": "recorder.do", "args": {}}],
}


def case_with(**overrides) -> dict:
    case = {
        "schema_version": 1,
        "id": "UNIT-001",
        "title": "编排器单元用例",
        "module": "unit",
        "priority": "P1",
        "tags": ["unit"],
        "environment": {"name": "local"},
        "steps": [{"id": "s1", "action": "recorder.do", "args": {}}],
    }
    case.update(overrides)
    return case


def run(config, adapters, case, **kw):
    from mtp_contracts.case_validator import require_valid

    require_valid(case)
    runner = make_runner(config, adapters, **kw)
    try:
        from mtp_platform.engine.evidence import EvidenceStore
        import threading

        from mtp_contracts.results import new_run_id

        run_id = new_run_id()
        store = EvidenceStore(ARTIFACTS, run_id, redact_keys=config.redact_keys())
        return runner.run_case(case, run_id=run_id, store=store, cancel_event=threading.Event())
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
def test_successful_case_passes(config):
    result = run(config, {"recorder": RecordingAdapter()}, case_with())
    assert result.status == RunState.PASSED
    assert [s.status for s in result.steps] == [StepStatus.PASSED]
    assert result.steps[0].attempts == 1
    assert result.steps[0].started_at and result.steps[0].finished_at
    assert result.steps[0].duration_ms >= 0


def test_failed_assertion_marks_case_failed(config):
    case = case_with(assertions=[{"id": "a1", "type": "equals", "actual": 1, "expected": 2}])
    result = run(config, {"recorder": RecordingAdapter()}, case)
    assert result.status == RunState.FAILED
    assert result.assertions[0]["passed"] is False
    assert result.first_failure()["kind"] == "assertion"


def test_failed_step_marks_case_failed(config):
    adapter = RecordingAdapter([{"fail": True}])
    result = run(config, {"recorder": adapter}, case_with())
    assert result.status == RunState.FAILED
    assert result.steps[0].error["code"] == "policy_denied"


def test_abort_policy_skips_remaining_steps(config):
    adapter = RecordingAdapter([{"fail": True}])
    case = case_with(
        steps=[
            {"id": "s1", "action": "recorder.do", "args": {}},
            {"id": "s2", "action": "recorder.do", "args": {}},
        ]
    )
    result = run(config, {"recorder": adapter}, case)
    assert result.steps[1].status == StepStatus.SKIPPED
    assert len(adapter.calls) == 1  # 第二步没有真的执行


def test_continue_policy_runs_remaining_steps(config):
    adapter = RecordingAdapter([{"fail": True}, {}])
    case = case_with(
        steps=[
            {"id": "s1", "action": "recorder.do", "args": {}, "on_failure": "continue"},
            {"id": "s2", "action": "recorder.do", "args": {}},
        ]
    )
    result = run(config, {"recorder": adapter}, case)
    assert result.steps[1].status == StepStatus.PASSED
    assert result.status == RunState.FAILED


# ---------------------------------------------------------------------------
# 重试与超时
# ---------------------------------------------------------------------------
def test_retry_succeeds_on_second_attempt(config):
    adapter = RecordingAdapter([{"fail": True}, {}])
    case = case_with(steps=[{"id": "s1", "action": "recorder.do", "args": {}, "retry": 1}])
    result = run(config, {"recorder": adapter}, case)

    assert result.steps[0].status == StepStatus.PASSED
    assert result.steps[0].attempts == 2
    assert len(adapter.calls) == 2


def test_retry_exhausted_records_all_attempts(config):
    adapter = RecordingAdapter([{"fail": True}, {"fail": True}, {"fail": True}])
    case = case_with(steps=[{"id": "s1", "action": "recorder.do", "args": {}, "retry": 2}])
    result = run(config, {"recorder": adapter}, case)

    assert result.steps[0].status == StepStatus.FAILED
    assert result.steps[0].attempts == 3


def test_step_timeout_is_enforced(config):
    adapter = RecordingAdapter([{"sleep": 3}])
    case = case_with(steps=[{"id": "slow", "action": "recorder.do", "args": {}, "timeout_sec": 0.3}])
    result = run(config, {"recorder": adapter}, case)

    assert result.status == RunState.FAILED
    assert result.steps[0].error["code"] == "timeout"


# ---------------------------------------------------------------------------
# 清理必定执行
# ---------------------------------------------------------------------------
def test_postconditions_run_even_when_steps_fail(config):
    adapter = RecordingAdapter([{"fail": True}, {}])
    case = case_with(
        steps=[{"id": "s1", "action": "recorder.do", "args": {}}],
        postconditions=[{"id": "cleanup", "action": "recorder.do", "args": {"phase": "cleanup"}}],
    )
    result = run(config, {"recorder": adapter}, case)

    assert result.status == RunState.FAILED
    phases = [s.phase for s in result.steps]
    assert "postconditions" in phases
    assert adapter.calls[-1][1].get("phase") == "cleanup"


def test_postconditions_run_even_when_step_raises(config):
    def boom(action, args):
        raise RuntimeError("适配器炸了")

    adapter = RecordingAdapter([boom, {}])
    case = case_with(
        steps=[{"id": "s1", "action": "recorder.do", "args": {}}],
        postconditions=[{"id": "cleanup", "action": "recorder.do", "args": {"phase": "cleanup"}}],
    )
    result = run(config, {"recorder": adapter}, case)

    assert result.status == RunState.FAILED
    assert any(s.phase == "postconditions" for s in result.steps)
    assert adapter.calls[-1][1].get("phase") == "cleanup"


def test_fixture_cleanup_always_runs_in_reverse(config):
    adapter = RecordingAdapter()
    case = case_with(
        fixtures=[
            {
                "id": "f1",
                "action": "recorder.do",
                "args": {"setup": 1},
                "cleanup": {"action": "recorder.do", "args": {"cleanup": "f1"}},
            },
            {
                "id": "f2",
                "action": "recorder.do",
                "args": {"setup": 2},
                "cleanup": {"action": "recorder.do", "args": {"cleanup": "f2"}},
            },
        ],
    )
    result = run(config, {"recorder": adapter}, case)

    assert result.status == RunState.PASSED
    cleanups = [c for c in result.cleanup if c["status"] == "passed"]
    assert len(cleanups) == 2
    # 倒序：后写入的先清理
    order = [c["step_id"] for c in cleanups]
    assert order == ["cleanup:f2", "cleanup:f1"]


def test_fixture_cleanup_failure_is_a_warning_by_default(config):
    adapter = RecordingAdapter([{}, {}, {"fail": True}])
    case = case_with(
        fixtures=[
            {
                "id": "f1",
                "action": "recorder.do",
                "args": {"setup": 1},
                "cleanup": {"action": "recorder.do", "args": {"cleanup": "f1"}},
            }
        ],
    )
    result = run(config, {"recorder": adapter}, case)

    assert result.status == RunState.PASSED
    assert any("清理失败" in w for w in result.warnings)


def test_fixture_audit_warns_when_no_run_scoping(config):
    """fixture 写数据却不带 run.id → 应当给出并行隔离的告警。"""
    adapter = RecordingAdapter([{}])
    case = case_with(
        fixtures=[
            {
                "id": "f1",
                "action": "mysql.insert",
                "args": {"table_name": "t", "row": {"a": 1}},
                "cleanup": {"action": "mysql.delete", "args": {"table_name": "t", "where": {"a": 1}}},
            }
        ],
    )
    result = run(config, {"recorder": adapter, "mysql": adapter}, case)
    assert any("run.id" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------
def test_cancel_between_steps(config):
    import threading

    from mtp_contracts.case_validator import require_valid
    from mtp_platform.engine.evidence import EvidenceStore
    from mtp_platform.engine.orchestrator import TestRunner
    from mtp_contracts.results import new_run_id

    adapter = RecordingAdapter()
    registry = FakeRegistry(config)
    registry.register("recorder", adapter)
    runner = TestRunner(config, registry=registry, artifacts_root=ARTIFACTS)
    cancel = threading.Event()

    case = require_valid(
        case_with(
            steps=[
                {"id": "s1", "action": "recorder.do", "args": {}},
                {"id": "s2", "action": "recorder.do", "args": {}},
            ]
        )
    )

    original = adapter._next

    def cancelling(action, args):
        cancel.set()  # 第一步执行时置位
        return original(action, args)

    adapter._next = cancelling

    try:
        run_id = new_run_id()
        store = EvidenceStore(ARTIFACTS, run_id, redact_keys=config.redact_keys())
        result = runner.run_case(case, run_id=run_id, store=store, cancel_event=cancel)
    finally:
        runner.close()

    assert result.status == RunState.CANCELLED
    assert result.steps[1].status == StepStatus.CANCELLED


# ---------------------------------------------------------------------------
# 变量与上下文
# ---------------------------------------------------------------------------
def test_variables_flow_into_args_and_assertions(config):
    adapter = RecordingAdapter([{}, {}])
    case = case_with(
        variables={"who": "tester"},
        environment={"name": "local", "base_url": "http://127.0.0.1:8099"},
        steps=[
            {"id": "s1", "action": "recorder.do", "args": {"who": "{{ vars.who }}"}},
        ],
        assertions=[
            {"id": "a1", "type": "equals", "actual": "{{ steps.s1.step_status }}", "expected": "passed"},
        ],
    )
    result = run(config, {"recorder": adapter}, case)

    assert adapter.calls[0][1]["who"] == "tester"
    assert result.assertions[0]["passed"]


def test_expect_failure_inverts_step_result(config):
    adapter = RecordingAdapter([{"fail": True}])
    case = case_with(steps=[{"id": "s1", "action": "recorder.do", "args": {}, "expect_failure": True}])
    result = run(config, {"recorder": adapter}, case)

    assert result.status == RunState.PASSED
    assert result.steps[0].data["expected_failure"] is True


def test_unexpected_success_fails_negative_case(config):
    adapter = RecordingAdapter([{}])
    case = case_with(steps=[{"id": "s1", "action": "recorder.do", "args": {}, "expect_failure": True}])
    result = run(config, {"recorder": adapter}, case)

    assert result.status == RunState.FAILED
    assert result.steps[0].error["code"] == "unexpected_success"
