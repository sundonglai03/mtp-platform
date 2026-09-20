"""构建统一的报告载荷（原始 JSON）。

JSON 是**唯一事实来源**：JUnit XML 与 HTML 都从这里派生，所以三者不会互相矛盾
（reporting/TASK 的验收标准：Office 报告与 JSON 结果一致）。

载荷里不放原文凭据：`CaseResult` 在编排阶段已经脱敏，这里只做汇总。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mtp_contracts.results import CaseResult, RunState

REPORT_SCHEMA_VERSION = 1


def _status_bucket(status: RunState) -> str:
    return {
        RunState.PASSED: "passed",
        RunState.FAILED: "failed",
        RunState.ERROR: "error",
        RunState.CANCELLED: "cancelled",
        RunState.QUEUED: "skipped",
        RunState.RUNNING: "skipped",
    }.get(status, "error")


def build_payload(
    results: list[CaseResult],
    *,
    run_id: str,
    started_at: str = "",
    finished_at: str = "",
    allow_write: bool = False,
    config_path: str = "",
) -> dict[str, Any]:
    cases = [r.to_dict() for r in results]

    buckets = {"passed": 0, "failed": 0, "error": 0, "cancelled": 0, "skipped": 0}
    for result in results:
        buckets[_status_bucket(result.status)] += 1

    total_duration = sum(r.duration_ms for r in results)
    assertions_total = sum(len(r.assertions) for r in results)
    assertions_failed = sum(1 for r in results for a in r.assertions if not a.get("passed"))

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "started_at": started_at,
        "finished_at": finished_at,
        "context": {
            "allow_write": allow_write,
            "config_path": config_path,
        },
        "summary": {
            "cases_total": len(results),
            "cases_passed": buckets["passed"],
            "cases_failed": buckets["failed"],
            "cases_error": buckets["error"],
            "cases_cancelled": buckets["cancelled"],
            "cases_skipped": buckets["skipped"],
            "assertions_total": assertions_total,
            "assertions_failed": assertions_failed,
            "duration_ms": total_duration,
            "success": (
                buckets["failed"] == 0
                and buckets["error"] == 0
                and buckets["cancelled"] == 0
            ),
        },
        "cases": cases,
    }


def status_line(payload: dict[str, Any]) -> str:
    s = payload["summary"]
    verdict = "PASS" if s["success"] else "FAIL"
    return (
        f"[{verdict}] run={payload['run_id']} "
        f"用例 {s['cases_passed']}/{s['cases_total']} 通过，"
        f"失败 {s['cases_failed']}，错误 {s['cases_error']}，"
        f"断言失败 {s['assertions_failed']}/{s['assertions_total']}，"
        f"耗时 {s['duration_ms']}ms"
    )
