"""Persistent, bounded background job manager."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

from mtp_contracts.results import now_iso

from .executor import RunExecutor, RunRequest
from .repository import TERMINAL_STATES, RunRepository


class JobManager:
    def __init__(
        self,
        *,
        repository: RunRepository,
        artifacts_root: Path,
        config_path: str | None,
        max_workers: int = 1,
    ) -> None:
        self.repository = repository
        self.artifacts_root = artifacts_root.resolve()
        self.config_path = config_path
        self.max_workers = max(1, max_workers)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._accepting = False
        self._workers: list[threading.Thread] = []
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        self.repository.recover_interrupted()
        self._accepting = True
        for index in range(self.max_workers):
            worker = threading.Thread(
                target=self._worker,
                name=f"mtp-job-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        for run_id in self.repository.queued_ids():
            self._queue.put(run_id)

    def stop(self) -> None:
        self._accepting = False
        self._stop.set()
        with self._lock:
            for cancel in self._active.values():
                cancel.set()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=15)

    def submit(
        self,
        *,
        run_id: str,
        uploads: list[Path],
        options: dict[str, Any],
        validation_errors: list[dict[str, Any]],
    ) -> None:
        if not self._accepting:
            raise RuntimeError("服务正在关闭，不能接收新任务")
        self.repository.create(
            run_id=run_id,
            uploads=[str(path) for path in uploads],
            options=options,
            validation_errors=validation_errors,
        )
        self._queue.put(run_id)

    def cancel(self, run_id: str) -> bool:
        run = self.repository.get(run_id)
        if not run or run["status"] in TERMINAL_STATES:
            return False
        if run["status"] == "queued":
            self.repository.update(run_id, status="cancelled", finished_at=now_iso())
            return True
        with self._lock:
            event = self._active.get(run_id)
            if event:
                event.set()
                return True
        return False

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if run_id is None:
                return
            try:
                self._execute(run_id)
            finally:
                self._queue.task_done()

    def _execute(self, run_id: str) -> None:
        cancel = threading.Event()
        with self._lock:
            self._active[run_id] = cancel
        try:
            if not self.repository.claim(run_id):
                return
            run = self.repository.get(run_id)
            if not run:
                return
            options = run["options"]
            run_root = (self.artifacts_root / "runs" / run_id).resolve()
            reports_root = run_root / "reports"

            def progress(result: Any, done: int, _total: int) -> None:
                current = self.repository.get(run_id)
                results = list(current["results"] if current else [])
                results.append(result.to_dict())
                self.repository.update(
                    run_id,
                    cases_done=done,
                    results_json=json.dumps(results, ensure_ascii=False),
                )

            outcome = RunExecutor().execute(
                RunRequest(
                    case_paths=[Path(path) for path in run["uploads"]],
                    config_path=self.config_path,
                    allow_write=bool(options.get("allow_write")),
                    office=bool(options.get("office")),
                    run_id=run_id,
                    artifacts_root=run_root,
                    output_dir=reports_root,
                    write_latest=False,
                ),
                cancel_event=cancel,
                on_progress=progress,
            )
            summary = outcome.payload["summary"]
            if cancel.is_set() or summary.get("cases_cancelled", 0):
                status = "cancelled"
            elif summary.get("cases_error", 0):
                status = "error"
            elif summary.get("cases_failed", 0):
                status = "failed"
            else:
                status = "passed"
            reports = {
                kind: path.name
                for kind, path in outcome.report_paths.items()
                if path is not None
            }
            self.repository.update(
                run_id,
                status=status,
                finished_at=now_iso(),
                cases_done=len(outcome.payload.get("cases", [])),
                results_json=json.dumps(
                    outcome.payload.get("cases", []), ensure_ascii=False
                ),
                summary_json=json.dumps(summary, ensure_ascii=False),
                reports_json=json.dumps(reports, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001 - job boundary must persist failure
            self.repository.update(
                run_id,
                status="error",
                finished_at=now_iso(),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            with self._lock:
                self._active.pop(run_id, None)
