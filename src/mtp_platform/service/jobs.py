"""Persistent background jobs backed by SQLite task state."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

from mtp_contracts.results import CaseResult, StepResult, now_iso

from .executor import RunExecutor, RunRequest, summarize
from .repository import TERMINAL_STATES, RunRepository

# 详情里单条字符串的上限。轮询型步骤（guard/poll）的 stdout 动辄上万字符，
# 不截断会把 SQLite 和详情接口一起撑大。
DETAIL_TEXT_LIMIT = 4000
# 单个用例详情序列化后的软上限；超过就丢掉步骤输出，只保留结构与结论。
DETAIL_CASE_LIMIT = 200_000


def _clip_text(value: str, limit: int = DETAIL_TEXT_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}…（已截断，完整 {len(value)} 字符）"


def _clip(node: Any) -> Any:
    """递归截断长字符串，保留结构。"""
    if isinstance(node, str):
        return _clip_text(node)
    if isinstance(node, dict):
        return {str(key): _clip(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_clip(item) for item in node]
    return node


def _failure_ref(result: CaseResult) -> dict[str, str] | None:
    """用例的第一个失败点，压成适合列表展示的短结构。"""
    failure = result.first_failure()
    if not failure:
        return None
    kind = str(failure.get("kind") or "case")
    if kind == "step":
        error = failure.get("error") or {}
        return {
            "kind": "step",
            "step_id": str(failure.get("step_id") or ""),
            "message": str(failure.get("summary") or error.get("message") or "步骤失败"),
        }
    if kind == "assertion":
        return {
            "kind": "assertion",
            "step_id": "",
            "message": str(
                failure.get("message")
                or f"断言 {failure.get('id')} 期望 {failure.get('expected')!r}，实际 {failure.get('actual')!r}"
            ),
        }
    return {"kind": kind, "step_id": "", "message": str(failure.get("message") or "用例执行失败")}


def _case_result(result: CaseResult) -> dict[str, Any]:
    """列表用的用例摘要：状态、耗时、断言计数、第一条失败点。"""
    return {
        "case_id": result.case_id,
        "title": result.title,
        "status": result.status.value,
        "duration_ms": result.duration_ms,
        "counts": result.counts(),
        "first_failure": _failure_ref(result),
    }


def _evidence_url(run_id: str, path: str) -> str:
    """证据的 HTTP 访问地址。"""
    return f"/api/runs/{run_id}/evidence/{path}"


def _attach_evidence_urls(run_id: str, items: Any) -> list[Any]:
    """给证据条目补上可访问地址。

    步骤级证据原先只有 `path`，详情页得自己拼 URL，漏掉就会渲染成
    「该证据没有可访问的文件」。统一在这里补上；历史任务的明细里没有这个字段，
    前端另做了按 path 兜底的拼接。
    """
    out: list[Any] = []
    for item in items or []:
        if isinstance(item, dict) and item.get("path"):
            out.append({**item, "url": _evidence_url(run_id, str(item["path"]))})
        else:
            out.append(item)
    return out


def _step_detail(step: StepResult) -> dict[str, Any]:
    payload = _clip(step.to_dict())
    # 这三个是给变量解析用的内部字段，详情里没必要暴露
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("step_ok", "step_status", "step_id"):
            data.pop(key, None)
    return payload


def _case_details(results: list[CaseResult]) -> dict[str, dict[str, Any]]:
    """逐用例的完整明细，按 case_id 索引后落库，供详情接口按需读取。"""
    details: dict[str, dict[str, Any]] = {}
    for result in results:
        steps = [_step_detail(step) for step in result.steps]
        for step in steps:
            step["evidence"] = _attach_evidence_urls(result.run_id, step.get("evidence"))
        cleanup = _clip(result.cleanup)
        for step in cleanup:
            if isinstance(step, dict):
                step["evidence"] = _attach_evidence_urls(result.run_id, step.get("evidence"))
        detail: dict[str, Any] = {
            "case_id": result.case_id,
            "title": result.title,
            "module": result.module,
            "priority": result.priority,
            "tags": list(result.tags),
            "status": result.status.value,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "duration_ms": result.duration_ms,
            "counts": result.counts(),
            "steps": steps,
            "assertions": _clip(result.assertions),
            "cleanup": cleanup,
            "warnings": [_clip_text(str(item)) for item in result.warnings],
            "error": _clip(result.error),
            "evidence": _attach_evidence_urls(result.run_id, result.evidence),
        }
        if len(json.dumps(detail, ensure_ascii=False)) > DETAIL_CASE_LIMIT:
            for step in detail["steps"]:
                step["data"] = {"truncated": True}
            detail["truncated"] = True
        details[result.case_id] = detail
    return details


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
    """证据索引。

    除路径外还带上 `case_id / step_id / kind`：详情页要按「用例 → 步骤」分组折叠，
    光靠从路径里切字符串太脆（`_safe()` 会把非法字符压成 `-`）。
    """
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
                    "url": _evidence_url(result.run_id, path),
                    "case_id": str(item.get("case_id") or result.case_id),
                    "step_id": str(item.get("step_id") or ""),
                    "kind": str(item.get("kind") or ""),
                    "summary": str(item.get("summary") or ""),
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
                    case_details_json=json.dumps(_case_details(results), ensure_ascii=False),
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
                case_details_json=json.dumps(_case_details(outcome.results), ensure_ascii=False),
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
