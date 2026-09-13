"""case-schema 校验器测试。"""

from pathlib import Path

import pytest

from mtp_platform.contracts.case_validator import iter_steps, load_case, validate_case, validate_file

CASES = Path(__file__).resolve().parent / "cases"
VALID = sorted((CASES / "valid").glob("*.yaml"))
INVALID = sorted((CASES / "invalid").glob("*.yaml"))


def test_case_files_present():
    assert len(VALID) == 3, [p.name for p in VALID]
    assert len(INVALID) == 3, [p.name for p in INVALID]


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_valid_cases_pass(path):
    result = validate_file(path)
    assert result.ok, result.messages()


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_invalid_cases_rejected(path):
    result = validate_file(path)
    assert not result.ok
    # 每个问题都必须带字段路径（验收标准：缺字段时能指出具体位置）
    assert result.issues
    for issue in result.issues:
        assert issue.path, f"问题缺少字段路径: {issue.message}"


def test_missing_steps_reports_path():
    result = validate_file(CASES / "invalid" / "missing-steps.yaml")
    assert any("steps" in i.path for i in result.issues)


def test_undeclared_refs_are_specific():
    result = validate_file(CASES / "invalid" / "undeclared-ref.yaml")
    rendered = "\n".join(result.messages())
    assert "steps.nope" in rendered
    assert "vars.undefined" in rendered
    assert "foo" in rendered  # 未知命名空间
    for issue in result.issues:
        assert issue.path


def test_plaintext_secret_rejected():
    result = validate_file(CASES / "invalid" / "plaintext-secret.yaml")
    assert any(i.path.endswith("password") for i in result.issues)


def test_duplicate_step_ids_detected():
    case = {
        "schema_version": 1,
        "id": "DUP-001",
        "title": "重复 id",
        "steps": [
            {"id": "same", "action": "playwright.snapshot"},
            {"id": "same", "action": "playwright.snapshot"},
        ],
    }
    result = validate_case(case)
    assert not result.ok
    assert any("重复" in i.message for i in result.issues)


def test_unknown_adapter_action_shape_rejected():
    case = {
        "schema_version": 1,
        "id": "BAD-ACTION",
        "title": "action 格式不对",
        "steps": [{"id": "s1", "action": "navigate"}],
    }
    result = validate_case(case)
    assert not result.ok
    assert any(i.path == "steps[0].action" for i in result.issues)


def test_assertion_id_required_only_at_top_level():
    case = {
        "schema_version": 1,
        "id": "GRP-001",
        "title": "分组断言",
        "steps": [{"id": "s1", "action": "playwright.snapshot"}],
        "assertions": [
            {
                "id": "group",
                "type": "any",
                "items": [{"type": "equals", "actual": 1, "expected": 1}],
            }
        ],
    }
    assert validate_case(case).ok


def test_schema_version_guard():
    case = {
        "schema_version": 99,
        "id": "V-001",
        "title": "版本不支持",
        "steps": [{"id": "s1", "action": "playwright.snapshot"}],
    }
    result = validate_case(case)
    assert not result.ok


def test_iter_steps_includes_fixtures_first():
    case = load_case(CASES / "valid" / "web-login.yaml")
    phases = [phase for phase, _, _ in iter_steps(case)]
    assert phases[0] == "fixtures"
    assert "steps" in phases and "preconditions" in phases and "postconditions" in phases
