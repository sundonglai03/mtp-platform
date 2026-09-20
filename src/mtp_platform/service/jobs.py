"""Persistent background jobs backed by SQLite task state."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

from mtp_contracts.results import CaseResult, now_iso

from .executor import RunExecutor, RunRequest, summarize
from .repository import TERMINAL_STATES, RunRepository


def _case_result(result: CaseResult) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "status": result.status.value,
        "duration_ms": result.duration_ms,
    }


def _first_failure(results: list[CaseResult]) -> dict[str, str | None] | None:
    for result in results:
        if result.passed:
            continue
        failure = result.first_failure() or {}
        return {
            "case_id": result.case_id,
            "step_id": failure.get("step_id"),
            "message": str(
                failure.get("message")
                or failure.get("summary")
                or (result.error or {}).get("message")
                or "用例执行失败"
            ),
        }
    return None


def _evidence(results: list[CaseResult]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        for item in result.evidence:
            path = str(item.get("path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            entries.append(
                {
                    "path": path,
                    "mime_type": str(item.get("mime_type") or "application/octet-stream"),
                    "size": int(item.get("bytes") or 0),
                }
            )
    return entries


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
            worker = threading.Thread(target=self._worker, name=f"mtp-job-{index + 1}", daemon=True)
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

    def submit(self, *, run_id: str, uploads: list[Path], allow_write: bool) -> None:
        if not self._accepting:
            raise RuntimeError("服务正在关闭，不能接收新任务")
        self.repository.create(
            run_id=run_id,
            uploads=[str(path) for path in uploads],
            options={"allow_write": allow_write},
        )
        self._queue.put(run_id)

    def cancel(self, run_id: str) -> bool:
        run = self.repository.get(run_id)
        if not run or run["status"] in TERMINAL_STATES:
            return False
        if run["status"] == "queued":
            self.repository.update(
                run_id,
                status="cancelled",
                finished_at=now_iso(),
                summary_json=json.dumps({"passed": 0, "failed": 0, "error": 0, "cancelled": 1}),
            )
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
        results: list[CaseResult] = []
        try:
            if not self.repository.claim(run_id):
                return
            run = self.repository.get(run_id)
            if not run:
                return

            def progress(result: CaseResult, done: int, _total: int) -> None:
                results.append(result)
                self.repository.update(
                    run_id,
                    cases_done=done,
                    cases_json=json.dumps([_case_result(item) for item in results], ensure_ascii=False),
                    summary_json=json.dumps(summarize(results, cancelled=False), ensure_ascii=False),
                    first_failure_json=json.dumps(_first_failure(results), ensure_ascii=False),
                    evidence_json=json.dumps(_evidence(results), ensure_ascii=False),
                )

            outcome = RunExecutor().execute(
                RunRequest(
                    case_paths=[Path(path) for path in run["uploads"]],
                    config_path=self.config_path,
                    allow_write=bool(run["options"].get("allow_write")),
                    run_id=run_id,
                    artifacts_root=self.artifacts_root / "runs" / run_id,
                ),
                cancel_event=cancel,
                on_progress=progress,
            )
            if cancel.is_set() or outcome.summary["cancelled"]:
                status = "cancelled"
            elif outcome.summary["error"]:
                status = "error"
            elif outcome.summary["failed"]:
                status = "failed"
            else:
                status = "passed"
            self.repository.update(
                run_id,
                status=status,
                finished_at=now_iso(),
                cases_done=len(outcome.results),
                cases_json=json.dumps([_case_result(item) for item in outcome.results], ensure_ascii=False),
                summary_json=json.dumps(outcome.summary, ensure_ascii=False),
                first_failure_json=json.dumps(_first_failure(outcome.results), ensure_ascii=False),
                evidence_json=json.dumps(_evidence(outcome.results), ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001 - job boundary must persist failure
            message = f"{type(exc).__name__}: {exc}"
            self.repository.update(
                run_id,
                status="error",
                finished_at=now_iso(),
                error=message,
                first_failure_json=json.dumps(
                    {"case_id": None, "step_id": None, "message": message},
                    ensure_ascii=False,
                ),
            )
        finally:
            with self._lock:
                self._active.pop(run_id, None)
