"""原始 JSON 报告。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .report_payload import build_payload


def write_json(
    results: list,
    out_dir: str | Path,
    *,
    run_id: str,
    started_at: str = "",
    finished_at: str = "",
    allow_write: bool = False,
    config_path: str = "",
    filename: str = "results.json",
    force_failure: bool = False,
) -> dict[str, Any]:
    """写 `results.json`，返回载荷本身（调用方还要用它喂 JUnit / HTML）。"""
    payload = build_payload(
        results,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        allow_write=allow_write,
        config_path=config_path,
    )
    if force_failure:
        payload["summary"]["success"] = False

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    payload["_written_to"] = str(target)
    return payload
