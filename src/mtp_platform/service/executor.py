"""One authoritative execution path for CLI and Web initiated runs."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mtp_contracts.results import CaseResult, new_run_id, now_iso

from mtp_platform.audit import AuditLog
from mtp_platform.config import load_config
from mtp_platform.engine import TestRunner
from mtp_platform.reporting import (
    append as append_history,
)
from mtp_platform.reporting import (
    status_line,
    write_html,
    write_json,
    write_junit,
    write_office_reports,
)
from mtp_platform.tools.registry import ToolRegistry

ProgressCallback = Callable[[CaseResult, int, int], None]
MessageCallback = Callable[[str, bool], None]
_HISTORY_LOCK = threading.Lock()


@dataclass(slots=True)
class RunRequest:
    case_paths: list[Path]
    config_path: str | None = None
    allow_write: bool = False
    office: bool = False
    run_id: str | None = None
    artifacts_root: Path | None = None
    output_dir: Path | None = None
    write_latest: bool = True


@dataclass(slots=True)
class RunOutcome:
    run_id: str
    payload: dict
    report_paths: dict[str, Path | None]
    office_errors: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.payload["summary"]["success"] else 1


class RunExecutor:
    """Execute cases, write all reports, history and audit records."""

    def execute(
        self,
        request: RunRequest,
        *,
        cancel_event: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        emit: MessageCallback | None = None,
    ) -> RunOutcome:
        config = load_config(request.config_path)
        run_id = request.run_id or new_run_id()
        started_at = now_iso()
        artifacts_root = Path(request.artifacts_root or config.artifact_root())
        out_dir = Path(request.output_dir or config.report_dir())
        cancel = cancel_event or threading.Event()

        audit = AuditLog(
            artifacts_root / "_audit" / f"{run_id}.jsonl",
            redact_keys=config.redact_keys(),
            placeholder=config.redact_placeholder(),
        )
        audit.run_start(
            run_id=run_id,
            cases=[str(path) for path in request.case_paths],
            allow_write=request.allow_write,
        )

        runner = TestRunner(
            config,
            registry=ToolRegistry(config),
            allow_write=request.allow_write,
            artifacts_root=artifacts_root,
            audit=audit,
        )
        results: list[CaseResult] = []
        try:
            total = len(request.case_paths)
            for index, path in enumerate(request.case_paths, start=1):
                if cancel.is_set():
                    break
                if emit:
                    emit(f"▶  执行 {path}", False)
                result = runner.run_case_file(path, run_id=run_id, cancel_event=cancel)
                results.append(result)
                if emit:
                    mark = {
                        "passed": "PASS",
                        "failed": "FAIL",
                        "error": "ERR ",
                        "cancelled": "CANC",
                    }.get(result.status.value, result.status.value.upper())
                    emit(f"   [{mark}] {result.case_id}  {result.duration_ms}ms", False)
                    failure = result.first_failure()
                    if failure and not result.passed:
                        if failure.get("kind") == "step":
                            emit(
                                f"          失败步骤: {failure.get('step_id')} — {failure.get('summary')}",
                                False,
                            )
                        elif failure.get("kind") == "assertion":
                            emit(
                                f"          失败断言: {failure.get('id')} — {failure.get('message')}",
                                False,
                            )
                        else:
                            emit(f"          用例错误: {failure.get('message')}", False)
                    for warning in result.warnings:
                        emit(f"          ⚠ {warning}", False)
                if on_progress:
                    on_progress(result, index, total)
        finally:
            runner.close()

        finished_at = now_iso()
        payload = write_json(
            results,
            out_dir,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            allow_write=request.allow_write,
            config_path=str(config.path),
            filename=f"{run_id}-results.json",
            force_failure=cancel.is_set(),
        )
        junit_path = write_junit(
            results, out_dir, run_id=run_id, filename=f"{run_id}-junit.xml"
        )
        html_path = write_html(payload, out_dir, filename=f"{run_id}-report.html")

        office: dict = {"xlsx": None, "docx": None, "errors": []}
        if request.office:
            office = write_office_reports(
                payload, out_dir, filename_prefix=f"{run_id}-report"
            )
            if emit:
                for item in office["errors"]:
                    emit(f"⚠  {item['kind']} 报告生成失败: {item['error']}", True)

        paths: dict[str, Path | None] = {
            "json": Path(payload["_written_to"]),
            "junit": Path(junit_path),
            "html": Path(html_path),
            "xlsx": Path(office["xlsx"]) if office["xlsx"] else None,
            "docx": Path(office["docx"]) if office["docx"] else None,
        }
        if request.write_latest:
            latest = out_dir / "latest"
            latest.mkdir(parents=True, exist_ok=True)
            for source, name in (
                (paths["json"], "results.json"),
                (paths["junit"], "junit.xml"),
                (paths["html"], "report.html"),
                (paths["xlsx"], "report.xlsx"),
                (paths["docx"], "report.docx"),
            ):
                if source:
                    shutil.copyfile(source, latest / name)

        with _HISTORY_LOCK:
            append_history(payload, config.history_file())
        audit.run_end(
            run_id=run_id,
            status="success" if payload["summary"]["success"] else "failure",
            duration_ms=payload["summary"]["duration_ms"],
            summary=payload["summary"],
        )
        return RunOutcome(
            run_id=run_id,
            payload=payload,
            report_paths=paths,
            office_errors=list(office["errors"]),
        )


def format_outcome(outcome: RunOutcome, *, office_requested: bool) -> list[str]:
    """Keep the established CLI summary format outside of the execution core."""
    paths = outcome.report_paths
    lines = [
        "",
        status_line(outcome.payload),
        f"JSON : {paths['json']}",
        f"JUnit: {paths['junit']}",
        f"HTML : {paths['html']}",
    ]
    if paths["xlsx"]:
        lines.append(f"Excel: {paths['xlsx']}")
    if paths["docx"]:
        lines.append(f"Word : {paths['docx']}")
    if office_requested and not (paths["xlsx"] and paths["docx"]):
        lines.append("（Office 报告不完整，见上面的告警）")
    return lines
