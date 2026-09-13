"""断言引擎测试。

覆盖 assertions/TASK 的验收标准：
- 相同输入始终得到相同结果（确定性）；
- 每条断言返回 passed/actual/expected/message；
- 失败消息可以直接进报告；
- 敏感值不会写入结果。
"""

from __future__ import annotations

import pytest

from mtp_platform.engine.assertions import AssertionEngine
from mtp_platform.contracts.redaction import SecretRegistry


@pytest.fixture()
def engine():
    return AssertionEngine()


def evaluate(engine: AssertionEngine, assertion: dict, context: dict | None = None):
    return engine.evaluate(assertion, context or {}, path="assertions[0]")


# ---------------------------------------------------------------------------
# 基础比较类
# ---------------------------------------------------------------------------
def test_equals_numeric_string_coercion(engine):
    assert evaluate(engine, {"id": "a", "type": "equals", "actual": "200", "expected": 200}).passed
    assert not evaluate(engine, {"id": "a", "type": "equals", "actual": "201", "expected": 200}).passed


def test_equals_boolean_strictness(engine):
    result = evaluate(engine, {"id": "a", "type": "equals", "actual": True, "expected": "true"})
    assert not result.passed


def test_equals_null(engine):
    assert evaluate(engine, {"id": "a", "type": "equals", "actual": None, "expected": None}).passed


def test_contains_string_and_list(engine):
    assert evaluate(engine, {"id": "a", "type": "contains", "actual": "hello world", "expected": "world"}).passed
    assert evaluate(engine, {"id": "a", "type": "contains", "actual": ["x", "y"], "expected": "y"}).passed
    assert not evaluate(engine, {"id": "a", "type": "contains", "actual": "hello", "expected": "zzz"}).passed


def test_status_code_reads_step_scalar(engine):
    context = {"steps": {"call": {"http_status": 200, "ok": True}}}
    result = evaluate(
        engine,
        {"id": "s", "type": "status_code", "actual": "{{ steps.call }}", "expected": 200},
        context,
    )
    assert result.passed
    assert result.actual == 200  # 自动从步骤结果里取标量


def test_response_time_modes(engine):
    context = {"steps": {"call": {"duration_ms": 250}}}
    base = {"id": "t", "type": "response_time", "actual": "{{ steps.call }}", "expected": 300}
    assert evaluate(engine, {**base, "args": {"mode": "lte"}}, context).passed
    assert not evaluate(engine, {**base, "args": {"mode": "gt"}}, context).passed
    assert evaluate(engine, {**base, "args": {"mode": "lt"}, "expected": 300}, context).passed


def test_exit_code(engine):
    context = {"steps": {"run": {"exit_code": 0}}}
    assert evaluate(engine, {"id": "e", "type": "exit_code", "actual": "{{ steps.run }}", "expected": 0}, context).passed


# ---------------------------------------------------------------------------
# JSON 相关
# ---------------------------------------------------------------------------
def test_json_path_match_and_miss(engine):
    context = {"steps": {"call": {"json": {"data": {"id": 7}}}}}
    ok = evaluate(
        engine,
        {"id": "j", "type": "json_path", "source": "{{ steps.call.json }}", "args": {"path": "$.data.id", "expected": 7}},
        context,
    )
    assert ok.passed

    miss = evaluate(
        engine,
        {"id": "j", "type": "json_path", "source": "{{ steps.call.json }}", "args": {"path": "$.data.nope", "expected": 1}},
        context,
    )
    assert not miss.passed
    assert "没有匹配" in miss.message


def test_json_schema(engine):
    context = {"steps": {"call": {"json": {"ok": True, "token": "abcdefgh"}}}}
    schema = {
        "type": "object",
        "required": ["ok", "token"],
        "properties": {"ok": {"const": True}, "token": {"type": "string", "minLength": 8}},
    }
    good = evaluate(
        engine,
        {"id": "k", "type": "json_schema", "source": "{{ steps.call.json }}", "args": {"schema": schema}},
        context,
    )
    assert good.passed

    bad = evaluate(
        engine,
        {
            "id": "k",
            "type": "json_schema",
            "source": "{{ steps.call.json }}",
            "args": {"schema": {**schema, "properties": {"ok": {"const": False}}}},
        },
        context,
    )
    assert not bad.passed


# ---------------------------------------------------------------------------
# 页面 / 文件 / 数据库
# ---------------------------------------------------------------------------
def test_page_text_contains_uses_source(engine):
    context = {"steps": {"page": {"page_text": "欢迎，tester"}}}
    assert evaluate(
        engine,
        {"id": "p", "type": "page_text_contains", "source": "{{ steps.page }}", "expected": "欢迎"},
        context,
    ).passed
    assert not evaluate(
        engine,
        {"id": "p", "type": "page_text_contains", "source": "{{ steps.page }}", "expected": "再见"},
        context,
    ).passed


def test_page_text_contains_falls_back_to_probe():
    calls = []

    def probe(action, args):
        calls.append(action)
        return {"ok": True, "data": {"page_text": "当前页面文本 登录成功"}, "error": None}

    engine = AssertionEngine(probe=probe)
    result = evaluate(engine, {"id": "p", "type": "page_text_contains", "expected": "登录成功"})
    assert result.passed
    assert calls == ["playwright.snapshot"]


def test_element_visible_uses_probe():
    def probe(action, args):
        assert action == "playwright.evaluate"
        assert "#logout" in args["function"]
        return {"ok": True, "data": {"json": {"visible": True, "reason": "ok"}}, "error": None}

    engine = AssertionEngine(probe=probe)
    result = evaluate(engine, {"id": "v", "type": "element_visible", "args": {"target": "#logout"}})
    assert result.passed


def test_element_visible_without_probe_reports_clearly(engine):
    result = evaluate(engine, {"id": "v", "type": "element_visible", "args": {"target": "#x"}})
    assert not result.passed
    assert "探针" in result.message


def test_file_exists(engine):
    """用仓库里真实存在的文件做断言，避免在测试里写盘。"""
    from pathlib import Path

    existing = Path(__file__).resolve().parent.parent / "mtp_config.yaml"
    missing = existing.parent / "__definitely_missing__.txt"

    assert evaluate(engine, {"id": "f", "type": "file_exists", "args": {"path": str(existing)}}).passed
    assert not evaluate(engine, {"id": "f", "type": "file_exists", "args": {"path": str(missing)}}).passed
    assert evaluate(
        engine, {"id": "f", "type": "file_exists", "args": {"path": str(existing), "min_bytes": 10}}
    ).passed
    assert not evaluate(
        engine, {"id": "f", "type": "file_exists", "args": {"path": str(existing), "min_bytes": 10_000_000}}
    ).passed


def test_db_value_extracts_row_column(engine):
    context = {"steps": {"q": {"rows": [{"username": "tester", "status": "active"}], "row_count": 1}}}
    ok = evaluate(
        engine,
        {
            "id": "d",
            "type": "db_value",
            "actual": "{{ steps.q }}",
            "expected": "tester",
            "args": {"row": 0, "column": "username"},
        },
        context,
    )
    assert ok.passed
    assert ok.actual == "tester"


def test_db_value_row_out_of_range(engine):
    context = {"steps": {"q": {"rows": [], "row_count": 0}}}
    result = evaluate(
        engine,
        {"id": "d", "type": "db_value", "actual": "{{ steps.q }}", "expected": "x", "args": {"row": 0, "column": "u"}},
        context,
    )
    assert not result.passed
    assert "取不到" in result.message


# ---------------------------------------------------------------------------
# 组合
# ---------------------------------------------------------------------------
def test_group_all_and_any(engine):
    all_pass = {
        "id": "g",
        "type": "all",
        "items": [
            {"type": "equals", "actual": 1, "expected": 1},
            {"type": "equals", "actual": 2, "expected": 2},
        ],
    }
    assert evaluate(engine, all_pass).passed

    all_mixed = {
        "id": "g",
        "type": "all",
        "items": [
            {"type": "equals", "actual": 1, "expected": 1},
            {"type": "equals", "actual": 2, "expected": 3},
        ],
    }
    result = evaluate(engine, all_mixed)
    assert not result.passed
    assert len(result.children) == 2

    any_mixed = {**all_mixed, "type": "any"}
    assert evaluate(engine, any_mixed).passed

    assert not evaluate(engine, {"id": "g", "type": "any", "items": [{"type": "equals", "actual": 1, "expected": 9}]}).passed


# ---------------------------------------------------------------------------
# 健壮性与脱敏
# ---------------------------------------------------------------------------
def test_unknown_type_reports_clearly(engine):
    result = evaluate(engine, {"id": "u", "type": "telepathy", "actual": 1, "expected": 1})
    assert not result.passed
    assert "不支持的断言类型" in result.message


def test_undefined_variable_does_not_crash(engine):
    result = evaluate(engine, {"id": "x", "type": "equals", "actual": "{{ steps.nope.a }}", "expected": 1})
    assert not result.passed
    assert result.error


def test_custom_message_is_used(engine):
    result = evaluate(
        engine,
        {"id": "m", "type": "equals", "actual": 1, "expected": 2, "message": "用户 ID 应一致"},
    )
    assert result.message.startswith("用户 ID 应一致")


def test_sensitive_values_are_scrubbed_from_result():
    registry = SecretRegistry()
    registry.register("SuperSecret123")
    engine = AssertionEngine(registry=registry)

    result = evaluate(
        engine,
        {"id": "s", "type": "equals", "actual": "SuperSecret123", "expected": "other"},
    )
    assert not result.passed
    assert "SuperSecret123" not in str(result.actual)
    assert "SuperSecret123" not in result.message


def test_engine_is_deterministic():
    engine = AssertionEngine()
    assertion = {"id": "d", "type": "equals", "actual": 1, "expected": 1}
    results = {evaluate(engine, assertion).passed for _ in range(20)}
    assert results == {True}
