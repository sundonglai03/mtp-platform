"""JUnit XML 报告。

为什么是它：Jenkins / GitLab CI / pytest 生态都能直接读，CI 里不用写解析代码。
（reporting/TASK 验收标准：Jenkins/GitLab 可以读取 JUnit XML）

约定：
- 每个 **module** 一个 `<testsuite>`；
- 每个 **用例** 一个 `<testcase>`；
- 步骤/断言失败 → `<failure>`；平台错误 → `<error>`；取消 → `<skipped>`；
- 失败详情里带上「哪一步、哪条断言、证据文件在哪」，CI 页面直接能定位。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..contracts.results import CaseResult, RunState

_STATUS_TO_TAG = {
    RunState.FAILED: "failure",
    RunState.ERROR: "error",
    RunState.CANCELLED: "skipped",
    RunState.QUEUED: "skipped",
    RunState.RUNNING: "skipped",
}


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def _failure_text(result: CaseResult) -> str:
    lines: list[str] = []
    failure = result.first_failure()
    if failure and failure.get("kind") == "step":
        lines.append(f"失败步骤: {failure.get('step_id')} ({failure.get('action')})")
        lines.append(f"阶段: {failure.get('phase')}  状态: {failure.get('status')}")
        lines.append(f"摘要: {failure.get('summary')}")
        err = failure.get("error") or {}
        if err:
            lines.append(f"错误码: {err.get('code')}")
            lines.append(f"错误信息: {err.get('message')}")
            if err.get("detail"):
                lines.append(f"详情: {str(err['detail'])[:1500]}")
    elif failure and failure.get("kind") == "assertion":
        lines.append(f"失败断言: {failure.get('id')} ({failure.get('type')})")
        lines.append(f"期望: {failure.get('expected')!r}")
        lines.append(f"实际: {failure.get('actual')!r}")
        lines.append(f"说明: {failure.get('message')}")
    elif result.error:
        lines.append(f"用例错误: {result.error.get('code')}")
        lines.append(str(result.error.get("message")))
        if result.error.get("detail"):
            lines.append(f"详情: {str(result.error['detail'])[:1500]}")

    for warning in result.warnings:
        lines.append(f"告警: {warning}")
    return "\n".join(lines)


def _system_out(result: CaseResult) -> str:
    lines: list[str] = []
    for step in result.steps:
        lines.append(
            f"[{step.status.value:<8}] {step.phase}/{step.step_id} ({step.action}) "
            f"{step.duration_ms}ms attempts={step.attempts} :: {step.summary}"
        )
    if result.assertions:
        lines.append("--- assertions ---")
        for assertion in result.assertions:
            mark = "OK  " if assertion.get("passed") else "FAIL"
            lines.append(f"[{mark}] {assertion.get('id')} {assertion.get('message')}")
    if result.evidence:
        lines.append("--- evidence ---")
        for item in result.evidence:
            location = item.get("path") or "(inline)"
            lines.append(f"{item.get('kind')} {item.get('id')} {location}")
    return "\n".join(lines)


def write_junit(
    results: list[CaseResult],
    out_dir: str | Path,
    *,
    run_id: str,
    filename: str = "junit.xml",
) -> Path:
    suites_by_module: dict[str, list[CaseResult]] = {}
    for result in results:
        suites_by_module.setdefault(result.module or "default", []).append(result)

    root = ET.Element(
        "testsuites",
        {
            "name": "my-test",
            "tests": str(len(results)),
            "failures": str(sum(1 for r in results if r.status == RunState.FAILED)),
            "errors": str(sum(1 for r in results if r.status == RunState.ERROR)),
            "skipped": str(sum(1 for r in results if r.status == RunState.CANCELLED)),
            "time": _seconds(sum(r.duration_ms for r in results)),
        },
    )

    for module, cases in sorted(suites_by_module.items()):
        suite = ET.SubElement(
            root,
            "testsuite",
            {
                "name": module,
                "tests": str(len(cases)),
                "failures": str(sum(1 for c in cases if c.status == RunState.FAILED)),
                "errors": str(sum(1 for c in cases if c.status == RunState.ERROR)),
                "skipped": str(sum(1 for c in cases if c.status == RunState.CANCELLED)),
                "time": _seconds(sum(c.duration_ms for c in cases)),
                "timestamp": cases[0].started_at or "",
            },
        )

        properties = ET.SubElement(suite, "properties")
        ET.SubElement(properties, "property", {"name": "run_id", "value": run_id})

        for case in cases:
            testcase = ET.SubElement(
                suite,
                "testcase",
                {
                    "classname": f"{module}.{case.case_id}",
                    "name": case.title or case.case_id,
                    "time": _seconds(case.duration_ms),
                },
            )
            if case.priority:
                ET.SubElement(testcase, "property", {"name": "priority", "value": case.priority})
            if case.tags:
                ET.SubElement(
                    testcase, "property", {"name": "tags", "value": ",".join(case.tags)}
                )

            tag = _STATUS_TO_TAG.get(case.status)
            if tag:
                failure = case.first_failure() or {}
                message = failure.get("message") or failure.get("summary") or ""
                if not isinstance(message, str) or not message:
                    message = case.status.value
                node = ET.SubElement(
                    testcase,
                    tag,
                    {"message": message[:300], "type": case.status.value},
                )
                node.text = _failure_text(case)

            out = ET.SubElement(testcase, "system-out")
            out.text = _system_out(case)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    tree.write(target, encoding="utf-8", xml_declaration=True)
    return target
