"""Execute JSON suite cases for the HTTP worker without producing reports."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mtp_contracts.results import CaseResult, new_run_id

from mtp_platform.config import load_config
from mtp_platform.engine import TestRunner
from mtp_platform.tools.registry import ToolRegistry

ProgressCallback = Callable[[CaseResult, int, int], None]


@dataclass(slots=True)
class RunRequest:
    case_paths: list[Path]
    config_path: str | None = None
    allow_write: bool = False
    run_id: str | None = None
    artifacts_root: Path | None = None


@dataclass(slots=True)
class RunOutcome:
    run_id: str
    results: list[CaseResult]
    summary: dict[str, int]


def summarize(results: list[CaseResult], *, cancelled: bool) -> dict[str, int]:
    """Return only the result counters displayed by the Web product."""
    counts = {"passed": 0, "failed": 0, "error": 0, "cancelled": 0}
    for result in results:
        if result.status.value in counts:
            counts[result.status.value] += 1
    if cancelled and not counts["cancelled"]:
        counts["cancelled"] = 1
    return counts


class RunExecutor:
    """Execute validated case files and retain only result objects for SQLite."""

    def execute(
        self,
        request: RunRequest,
        *,
        cancel_event: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> RunOutcome:
        config = load_config(request.config_path)
        run_id = request.run_id or new_run_id()
        artifacts_root = Path(request.artifacts_root or config.artifact_root())
        cancel = cancel_event or threading.Event()
        runner = TestRunner(
            config,
            registry=ToolRegistry(config),
            allow_write=request.allow_write,
            artifacts_root=artifacts_root,
        )
        results: list[CaseResult] = []
        try:
            total = len(request.case_paths)
            for index, path in enumerate(request.case_paths, start=1):
                if cancel.is_set():
                    break
                result = runner.run_case_file(path, run_id=run_id, cancel_event=cancel)
                results.append(result)
                if on_progress:
                    on_progress(result, index, total)
        finally:
            runner.close()
        return RunOutcome(run_id, results, summarize(results, cancelled=cancel.is_set()))
