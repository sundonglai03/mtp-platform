"""历史运行与趋势。

每次运行往 `artifacts/history.jsonl` 追加**一行汇总**（不写全量结果，避免文件无限膨胀），
用于回答「最近是不是越来越不稳定」。

行格式::

    {"run_id": "...", "at": "...", "cases_total": 3, "passed": 2, "failed": 1, ...}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _record(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary", {})
    return {
        "run_id": payload.get("run_id", ""),
        "at": payload.get("generated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "started_at": payload.get("started_at", ""),
        "finished_at": payload.get("finished_at", ""),
        "cases_total": summary.get("cases_total", 0),
        "passed": summary.get("cases_passed", 0),
        "failed": summary.get("cases_failed", 0),
        "error": summary.get("cases_error", 0),
        "cancelled": summary.get("cases_cancelled", 0),
        "assertions_total": summary.get("assertions_total", 0),
        "assertions_failed": summary.get("assertions_failed", 0),
        "duration_ms": summary.get("duration_ms", 0),
        "success": bool(summary.get("success")),
        "cases": [
            {"case_id": c.get("case_id"), "status": c.get("status")}
            for c in (payload.get("cases") or [])
        ],
    }


def append(payload: dict[str, Any], history_file: str | Path) -> Path:
    target = Path(history_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_record(payload), ensure_ascii=False) + "\n")
    return target


def read_all(history_file: str | Path) -> list[dict[str, Any]]:
    target = Path(history_file)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def trend(history_file: str | Path, *, limit: int = 10) -> dict[str, Any]:
    """最近 N 次的通过率与耗时趋势。"""
    records = read_all(history_file)[-limit:]
    if not records:
        return {"runs": 0, "pass_rate": None, "records": []}

    total = sum(r.get("cases_total", 0) for r in records)
    passed = sum(r.get("passed", 0) for r in records)
    return {
        "runs": len(records),
        "pass_rate": round(passed / total, 4) if total else None,
        "avg_duration_ms": int(sum(r.get("duration_ms", 0) for r in records) / len(records)),
        "records": records,
    }


def format_trend(history_file: str | Path, *, limit: int = 10) -> str:
    data = trend(history_file, limit=limit)
    if not data["runs"]:
        return "（暂无历史运行记录）"
    lines = [f"最近 {data['runs']} 次运行，用例通过率 {data['pass_rate']:.1%}，平均耗时 {data['avg_duration_ms']}ms"]
    for record in data["records"]:
        mark = "PASS" if record.get("success") else "FAIL"
        lines.append(
            f"  [{mark}] {record['run_id']}  "
            f"{record.get('passed', 0)}/{record.get('cases_total', 0)} 通过  "
            f"{record.get('duration_ms', 0)}ms  {record.get('at', '')}"
        )
    return "\n".join(lines)
