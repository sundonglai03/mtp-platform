"""契约层自身的测试：可序列化、可往返、版本守卫。

这一组测试不依赖 engine / adapters / platform —— 只验证契约本身稳定。
"""

from __future__ import annotations

import json

import pytest

from mtp_contracts import (
    ActionResult,
    CaseResult,
    ConfigError,
    MtpError,
    RunState,
    SecretRegistry,
    StepResult,
    StepStatus,
    TimeoutError_,
    classify_exception,
    redact,
)
from mtp_contracts.case_validator import SUPPORTED_SCHEMA_VERSIONS


def test_result_models_are_json_serializable():
    """结果模型必须能直接进 JSON（报告层只消费序列化结果）。"""
    step = StepResult(step_id="s1", action="playwright.snapshot", status=StepStatus.PASSED)
    case = CaseResult(run_id="20260913T000000-abcdef", case_id="C-1", steps=[step])
    case.status = RunState.PASSED

    payload = case.to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    restored = json.loads(text)

    assert restored["case_id"] == "C-1"
    assert restored["status"] == "passed"
    assert restored["steps"][0]["status"] == "passed"


def test_status_enum_roundtrip_by_value():
    for status in RunState:
        assert RunState(status.value) is status
    for status in StepStatus:
        assert StepStatus(status.value) is status


def test_run_state_terminal_set():
    assert RunState.PASSED.terminal
    assert RunState.FAILED.terminal
    assert not RunState.RUNNING.terminal
    assert not RunState.QUEUED.terminal


def test_case_result_counts_and_first_failure():
    case = CaseResult(run_id="r", case_id="c")
    case.assertions = [{"id": "a1", "passed": True}, {"id": "a2", "passed": False}]
    counts = case.counts()
    assert counts == {"assertions_total": 2, "assertions_passed": 1, "assertions_failed": 1}
    case.steps = [
        StepResult(step_id="s1", action="x.y", status=StepStatus.PASSED),
        StepResult(step_id="s2", action="x.y", status=StepStatus.FAILED, summary="boom"),
    ]
    failure = case.first_failure()
    assert failure is not None and failure["kind"] == "step"


def test_error_to_dict_is_stable_and_json_safe():
    error = MtpError("出错了", detail="d", adapter="ssh", action="execute")
    data = error.to_dict()
    assert data["code"] == "mtp_error"
    assert data["adapter"] == "ssh"
    json.dumps(data)


def test_classify_exception_maps_known_kinds():
    assert isinstance(classify_exception(TimeoutError("slow")), TimeoutError_)
    assert classify_exception(ValueError("x")).code == "tool_failed"
    # 已分类的 MtpError 原样返回，并补上 adapter/action
    original = ConfigError("bad")
    same = classify_exception(original, adapter="mysql", action="query")
    assert same is original and same.adapter == "mysql" and same.action == "query"


def test_redaction_by_key_and_by_value():
    registry = SecretRegistry()
    registry.register("super-secret-token")
    obj = {"password": "p@ss", "note": "echo super-secret-token"}
    out = redact(obj, registry=registry)
    assert out["password"] != "p@ss"
    assert "super-secret-token" not in out["note"]


def test_action_result_success_and_failure_dict():
    ok = ActionResult.success("navigate", adapter="playwright", data={"url": "http://x"})
    assert ok.ok and ok.to_dict()["adapter"] == "playwright"
    bad = ActionResult.failure("query", ConfigError("no db"), adapter="mysql")
    assert not bad.ok and bad.to_dict()["error"]["code"] == "config_error"


def test_schema_version_is_frozen():
    """schema 兼容守卫：当前只支持 v1；升级需显式改这里并加迁移测试。"""
    assert SUPPORTED_SCHEMA_VERSIONS == (1,)
