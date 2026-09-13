"""确定性断言引擎。

设计原则（对应 assertions/TASK 的验收标准）：

- **纯确定性**：不调用任何大模型，相同输入永远得到相同结果；
- **结果自带解释**：每条断言返回 `passed / actual / expected / message`，
  失败消息可以直接贴进报告，不需要人来二次解读；
- **敏感值不入结果**：`actual`/`expected` 在返回前统一走脱敏，
  所以「密码断言失败」不会把密码写进报告；
- **组合**：`all` / `any` 递归组合子断言。

需要**现场状态**的断言（`element_visible`）通过注入的 `probe` 回调向
Playwright 提问，而不是从快照里猜——这样结果才是确定性的。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..contracts.redaction import DEFAULT_PLACEHOLDER, DEFAULT_REDACT_KEYS, SecretRegistry, redact
from ..contracts.variables import resolve

# probe(action, args) -> {"ok": bool, "data": dict, "error": dict|None}
Probe = Callable[[str, dict], dict]

SCALAR_KEYS = {
    "exit_code": ("exit_code",),
    "status_code": ("http_status", "status_code", "status"),
    "response_time": ("duration_ms", "elapsed_ms", "response_time_ms"),
    "page_text_contains": ("page_text", "text", "body"),
}

_VISIBILITY_JS = """() => {
  const sel = %s;
  const el = document.querySelector(sel);
  if (!el) return { visible: false, reason: 'not-found' };
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  const visible = style.display !== 'none' && style.visibility !== 'hidden'
      && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
  return { visible, reason: visible ? 'ok' : 'not-rendered' };
}"""


@dataclass
class AssertionResult:
    id: str
    type: str
    passed: bool
    message: str
    actual: Any = None
    expected: Any = None
    severity: str = "major"
    children: list["AssertionResult"] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "actual": self.actual,
            "expected": self.expected,
            "duration_ms": self.duration_ms,
        }
        if self.children:
            out["children"] = [c.to_dict() for c in self.children]
        if self.error:
            out["error"] = self.error
        return out


class AssertionEngine:
    def __init__(
        self,
        *,
        probe: Probe | None = None,
        registry: SecretRegistry | None = None,
        redact_keys: list[str] | None = None,
        placeholder: str = DEFAULT_PLACEHOLDER,
    ) -> None:
        self.probe = probe
        self.registry = registry or SecretRegistry()
        self.redact_keys = list(redact_keys or DEFAULT_REDACT_KEYS)
        self.placeholder = placeholder

    # -- 对外 ---------------------------------------------------------------
    def evaluate_many(
        self, assertions: list[dict[str, Any]], context: dict[str, Any]
    ) -> list[AssertionResult]:
        return [self.evaluate(a, context, path=f"assertions[{i}]") for i, a in enumerate(assertions or [])]

    def evaluate(
        self, assertion: dict[str, Any], context: dict[str, Any], *, path: str = ""
    ) -> AssertionResult:
        started = time.monotonic()
        aid = str(assertion.get("id") or path or "assertion")
        atype = str(assertion.get("type") or "")
        severity = str(assertion.get("severity") or "major")

        try:
            result = self._dispatch(assertion, context, aid, atype, path)
        except Exception as exc:  # noqa: BLE001 - 断言自身出错不能炸掉整轮
            result = AssertionResult(
                id=aid,
                type=atype,
                passed=False,
                message=f"断言执行出错: {type(exc).__name__}: {exc}",
                severity=severity,
                error=f"{type(exc).__name__}: {exc}",
            )

        result.severity = severity
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.actual = self._scrub(result.actual)
        result.expected = self._scrub(result.expected)
        # 消息里也会带 actual/expected 的原文，必须一起脱敏
        result.message = self._scrub(result.message)
        return result

    # -- 内部 ---------------------------------------------------------------
    def _scrub(self, value: Any) -> Any:
        return redact(
            value,
            keys=self.redact_keys,
            placeholder=self.placeholder,
            registry=self.registry,
        )

    def _dispatch(
        self,
        assertion: dict[str, Any],
        context: dict[str, Any],
        aid: str,
        atype: str,
        path: str,
    ) -> AssertionResult:
        if atype in {"all", "any"}:
            return self._assert_group(assertion, context, aid)
        handler = getattr(self, f"_assert_{atype}", None)
        if handler is None:
            return AssertionResult(
                id=aid,
                type=atype,
                passed=False,
                message=f"不支持的断言类型: {atype!r}",
            )
        return handler(assertion, context, aid)

    # -- 取值 ---------------------------------------------------------------
    @staticmethod
    def _resolve_arg(assertion: dict[str, Any], key: str, context: dict[str, Any], default: Any = None) -> Any:
        if key not in assertion:
            return default
        return resolve(assertion[key], context, path=key)

    @staticmethod
    def _scalar(raw: Any, atype: str) -> Any:
        """`actual` 可能直接指到一个步骤结果（dict），这里按断言类型取标量。"""
        if not isinstance(raw, dict):
            return raw
        for key in SCALAR_KEYS.get(atype, ()):
            if key in raw:
                return raw[key]
        return raw

    @staticmethod
    def _as_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    def _compare_equality(self, actual: Any, expected: Any) -> bool:
        # 布尔不放宽：True 不应该等于 "true"，否则断言会悄悄失去意义
        if isinstance(actual, bool) or isinstance(expected, bool):
            return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
        a_num, e_num = self._as_number(actual), self._as_number(expected)
        if a_num is not None and e_num is not None:
            return a_num == e_num
        if isinstance(actual, (dict, list)) and isinstance(expected, (dict, list)):
            return actual == expected
        if actual is None or expected is None:
            return actual is None and expected is None
        return str(actual) == str(expected)

    # -- 具体断言 -----------------------------------------------------------
    def _assert_equals(self, a, ctx, aid) -> AssertionResult:
        actual = self._resolve_arg(a, "actual", ctx)
        expected = self._resolve_arg(a, "expected", ctx)
        passed = self._compare_equality(actual, expected)
        return AssertionResult(
            id=aid,
            type="equals",
            passed=passed,
            actual=actual,
            expected=expected,
            message=self._message(a, passed, f"期望等于 {expected!r}，实际 {actual!r}"),
        )

    def _assert_contains(self, a, ctx, aid) -> AssertionResult:
        actual = self._resolve_arg(a, "actual", ctx)
        expected = self._resolve_arg(a, "expected", ctx)
        if isinstance(actual, (list, tuple, set)):
            passed = any(self._compare_equality(item, expected) for item in actual)
        elif isinstance(actual, str):
            passed = str(expected) in actual
        elif actual is None:
            passed = False
        else:
            passed = str(expected) in str(actual)
        return AssertionResult(
            id=aid,
            type="contains",
            passed=passed,
            actual=actual,
            expected=expected,
            message=self._message(a, passed, f"期望包含 {expected!r}"),
        )

    def _assert_status_code(self, a, ctx, aid) -> AssertionResult:
        raw = self._resolve_arg(a, "actual", ctx)
        actual = self._scalar(raw, "status_code")
        expected = self._resolve_arg(a, "expected", ctx)
        passed = self._compare_equality(actual, expected)
        return AssertionResult(
            id=aid,
            type="status_code",
            passed=passed,
            actual=actual,
            expected=expected,
            message=self._message(a, passed, f"期望状态码 {expected}，实际 {actual}"),
        )

    def _assert_json_path(self, a, ctx, aid) -> AssertionResult:
        source = self._resolve_arg(a, "source", ctx)
        args = a.get("args") or {}
        path_expr = str(resolve(args.get("path", "$"), ctx))
        expected = resolve(args["expected"], ctx) if "expected" in args else None

        matches = _jsonpath_find(path_expr, source)
        if "expected" not in args:
            passed = bool(matches)
            actual: Any = matches
            message = self._message(a, passed, f"路径 {path_expr} 应有匹配")
        elif not matches:
            passed = False
            actual = None
            message = self._message(a, passed, f"路径 {path_expr} 没有匹配到任何值")
        else:
            actual = matches[0] if len(matches) == 1 else matches
            passed = self._compare_equality(actual, expected)
            message = self._message(a, passed, f"路径 {path_expr} 期望 {expected!r}，实际 {actual!r}")

        return AssertionResult(
            id=aid, type="json_path", passed=passed, actual=actual, expected=expected, message=message
        )

    def _assert_json_schema(self, a, ctx, aid) -> AssertionResult:
        source = self._resolve_arg(a, "source", ctx)
        args = a.get("args") or {}
        schema = args.get("schema")
        if schema is None and args.get("schema_file"):
            schema_path = Path(str(resolve(args["schema_file"], ctx)))
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            return AssertionResult(
                id=aid,
                type="json_schema",
                passed=False,
                message="json_schema 断言需要 args.schema 或 args.schema_file",
            )

        try:
            import jsonschema

            jsonschema.validate(instance=source, schema=schema)
            passed, detail = True, "结构符合预期"
        except ImportError:  # pragma: no cover
            passed, detail = False, "缺少 jsonschema 依赖"
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, str(exc).splitlines()[0][:300]

        return AssertionResult(
            id=aid,
            type="json_schema",
            passed=passed,
            actual=source,
            expected=schema,
            message=self._message(a, passed, detail),
        )

    def _assert_response_time(self, a, ctx, aid) -> AssertionResult:
        raw = self._resolve_arg(a, "actual", ctx)
        actual = self._scalar(raw, "response_time")
        expected = self._resolve_arg(a, "expected", ctx)
        args = a.get("args") or {}
        mode = str(args.get("mode", "lte")).lower()

        a_num, e_num = self._as_number(actual), self._as_number(expected)
        if a_num is None or e_num is None:
            passed = False
            detail = f"响应时间不可比较: actual={actual!r} expected={expected!r}"
        else:
            ops = {
                "lte": a_num <= e_num,
                "lt": a_num < e_num,
                "gte": a_num >= e_num,
                "gt": a_num > e_num,
                "eq": a_num == e_num,
            }
            passed = ops.get(mode, a_num <= e_num)
            detail = f"响应时间 {a_num:g}ms 应 {mode} {e_num:g}ms"

        return AssertionResult(
            id=aid,
            type="response_time",
            passed=passed,
            actual=actual,
            expected=expected,
            message=self._message(a, passed, detail),
        )

    def _assert_page_text_contains(self, a, ctx, aid) -> AssertionResult:
        raw = self._resolve_arg(a, "source", ctx)
        args = a.get("args") or {}
        text = self._scalar(raw, "page_text_contains")
        if text is None and self.probe is not None:
            probed = self.probe("playwright.snapshot", {})
            if probed.get("ok"):
                text = probed.get("data", {}).get("page_text")
        expected = self._resolve_arg(a, "expected", ctx)
        if expected is None:
            expected = resolve(args.get("text"), ctx) if "text" in args else None

        haystack = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        passed = bool(expected) and str(expected) in haystack
        return AssertionResult(
            id=aid,
            type="page_text_contains",
            passed=passed,
            actual="<page_text %d chars>" % len(haystack),
            expected=expected,
            message=self._message(a, passed, f"页面文本应包含 {expected!r}"),
        )

    def _assert_element_visible(self, a, ctx, aid) -> AssertionResult:
        args = a.get("args") or {}
        target = args.get("target") or args.get("selector")
        if not target:
            return AssertionResult(
                id=aid,
                type="element_visible",
                passed=False,
                message="element_visible 需要 args.target（CSS 选择器）",
            )
        target = str(resolve(target, ctx))

        if self.probe is None:
            return AssertionResult(
                id=aid,
                type="element_visible",
                passed=False,
                actual=None,
                expected=True,
                message="没有可用的浏览器探针，无法判断元素可见性",
                error="probe-unavailable",
            )

        timeout_ms = int(args.get("timeout_ms", 0) or 0)
        deadline = time.monotonic() + timeout_ms / 1000
        last: dict[str, Any] = {}
        while True:
            probed = self.probe(
                "playwright.evaluate",
                {"function": _VISIBILITY_JS % json.dumps(target)},
            )
            if not probed.get("ok"):
                return AssertionResult(
                    id=aid,
                    type="element_visible",
                    passed=False,
                    expected=True,
                    message=f"查询元素可见性失败: {probed.get('error') or '未知错误'}",
                    error=str(probed.get("error") or ""),
                )
            last = probed.get("data", {}).get("json") or {}
            if last.get("visible") or time.monotonic() >= deadline:
                break
            time.sleep(0.2)

        passed = bool(last.get("visible"))
        return AssertionResult(
            id=aid,
            type="element_visible",
            passed=passed,
            actual=last,
            expected={"visible": True},
            message=self._message(
                a, passed, f"元素 {target!r} 应可见（实际: {last.get('reason', 'unknown')}）"
            ),
        )

    def _assert_file_exists(self, a, ctx, aid) -> AssertionResult:
        args = a.get("args") or {}
        raw_path = args.get("path") or a.get("actual")
        target = Path(str(resolve(raw_path, ctx)))
        exists = target.exists()
        min_bytes = args.get("min_bytes")
        if exists and min_bytes is not None:
            size = target.stat().st_size
            passed = size >= int(min_bytes)
            detail = f"文件应存在且 ≥ {min_bytes} 字节（实际 {size} 字节）"
        else:
            passed = exists
            detail = f"文件应存在: {target}"
        return AssertionResult(
            id=aid,
            type="file_exists",
            passed=passed,
            actual={"path": str(target), "exists": exists},
            expected=True,
            message=self._message(a, passed, detail),
        )

    def _assert_exit_code(self, a, ctx, aid) -> AssertionResult:
        raw = self._resolve_arg(a, "actual", ctx)
        actual = self._scalar(raw, "exit_code")
        expected = self._resolve_arg(a, "expected", ctx)
        passed = self._compare_equality(actual, expected)
        return AssertionResult(
            id=aid,
            type="exit_code",
            passed=passed,
            actual=actual,
            expected=expected,
            message=self._message(a, passed, f"期望退出码 {expected}，实际 {actual}"),
        )

    def _assert_db_value(self, a, ctx, aid) -> AssertionResult:
        raw = self._resolve_arg(a, "actual", ctx)
        args = a.get("args") or {}
        expected = self._resolve_arg(a, "expected", ctx)

        column = args.get("column")
        row_index = int(args.get("row", 0) or 0)
        actual: Any = raw

        if isinstance(raw, dict) and column:
            rows = raw.get("rows")
            if isinstance(rows, list):
                if row_index >= len(rows):
                    return AssertionResult(
                        id=aid,
                        type="db_value",
                        passed=False,
                        actual=None,
                        expected=expected,
                        message=f"结果集只有 {len(rows)} 行，取不到第 {row_index} 行",
                    )
                actual = (
                    rows[row_index].get(str(column)) if isinstance(rows[row_index], dict) else None
                )
            else:
                actual = raw.get(str(column))
        elif isinstance(raw, dict) and "row_count" in raw and "rows" not in raw and expected is None:
            actual = raw.get("row_count")

        passed = self._compare_equality(actual, expected)
        return AssertionResult(
            id=aid,
            type="db_value",
            passed=passed,
            actual=actual,
            expected=expected,
            message=self._message(
                a, passed, f"数据库值 {'.'.join(str(x) for x in (column, row_index) if x is not None)} "
                f"期望 {expected!r}，实际 {actual!r}"
            ),
        )

    def _assert_group(self, a, ctx, aid) -> AssertionResult:
        atype = str(a.get("type"))
        items = a.get("items") or []
        children = [
            self.evaluate(item, ctx, path=f"{aid}.items[{i}]") for i, item in enumerate(items)
        ]
        if atype == "all":
            passed = all(c.passed for c in children)
            default = "所有子断言都应通过"
        else:
            passed = any(c.passed for c in children)
            default = "至少一个子断言应通过"

        failed = [c.id for c in children if not c.passed]
        detail = default if passed else f"{default}；未通过: {', '.join(failed)}"
        return AssertionResult(
            id=aid,
            type=atype,
            passed=passed,
            actual={"passed_children": len(children) - len(failed), "total": len(children)},
            expected={"mode": atype},
            message=self._message(a, passed, detail),
            children=children,
        )

    @staticmethod
    def _message(a: dict[str, Any], passed: bool, detail: str) -> str:
        custom = a.get("message")
        if passed:
            return str(custom) if custom else f"通过：{detail}"
        return f"{custom + '：' if custom else ''}{detail}"


def _jsonpath_find(expr: str, data: Any) -> list[Any]:
    """JSONPath 取多值。用 jsonpath-ng（ext 支持 $.. 与过滤）。"""
    if data is None:
        return []
    if expr in ("$", ""):
        return [data]
    try:
        from jsonpath_ng.ext import parse as ext_parse

        compiled = ext_parse(expr)
    except Exception:  # noqa: BLE001 - 退化为标准语法
        from jsonpath_ng import parse as std_parse

        compiled = std_parse(expr)
    return [match.value for match in compiled.find(data)]


def summarize(results: list[AssertionResult]) -> dict[str, int]:
    """给报告用的汇总。"""
    total = len(results)
    failed = sum(1 for r in results if not r.passed)
    return {"total": total, "passed": total - failed, "failed": failed}


def failed_only(results: list[AssertionResult]) -> list[AssertionResult]:
    return [r for r in results if not r.passed]
