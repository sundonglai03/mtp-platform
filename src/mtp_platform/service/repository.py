"""SQLite persistence for the minimal Web task result."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from mtp_contracts.results import now_iso

from . import json_safe

TERMINAL_STATES = {"passed", "failed", "error", "cancelled"}


class RunRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    uploads_json TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    cases_total INTEGER NOT NULL DEFAULT 0,
                    cases_done INTEGER NOT NULL DEFAULT 0,
                    cases_json TEXT NOT NULL DEFAULT '[]',
                    summary_json TEXT NOT NULL DEFAULT '{"passed": 0, "failed": 0, "error": 0, "cancelled": 0}',
                    first_failure_json TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    case_details_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                )
                """
            )
            # 旧版本有 reports/results 等字段，但没有最小结果所需的 cases、
            # first_failure、evidence 字段。只追加，不删除既有任务。
            existing = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            migrations = {
                "cases_json": "TEXT NOT NULL DEFAULT '[]'",
                "first_failure_json": "TEXT",
                "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
                # 逐用例的步骤/断言明细（按 case_id 索引）。列表接口不返回它，
                # 只有 /api/runs/{run_id}/cases/{case_id} 按需读取，避免列表响应变胖。
                "case_details_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, definition in migrations.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")

    def recover_interrupted(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE runs
                   SET status = 'error', finished_at = ?,
                       error = '服务重启导致运行中断，请重新提交任务'
                 WHERE status = 'running'
                """,
                (now_iso(),),
            )

    def create(
        self,
        *,
        run_id: str,
        uploads: list[str],
        options: dict[str, Any],
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO runs (
                    run_id, status, created_at, uploads_json, options_json, cases_total
                ) VALUES (?, 'queued', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now_iso(),
                    json_safe.dumps(uploads),
                    json_safe.dumps(options),
                    len(uploads),
                ),
            )

    def update(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "started_at",
            "finished_at",
            "cases_done",
            "cases_json",
            "summary_json",
            "first_failure_json",
            "evidence_json",
            "case_details_json",
            "error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported run fields: {sorted(unknown)}")
        if not fields:
            return
        columns = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [run_id]
        with self._connect() as db:
            db.execute(f"UPDATE runs SET {columns} WHERE run_id = ?", values)

    def claim(self, run_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE runs
                   SET status = 'running', started_at = ?, error = NULL
                 WHERE run_id = ? AND status = 'queued'
                """,
                (now_iso(), run_id),
            )
            return cursor.rowcount == 1

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._decode(row) if row else None

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def queued_ids(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT run_id FROM runs WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def purge_finished_before(self, cutoff: str) -> list[str]:
        """删除保留期之前的终态任务，返回需要同步清理的目录 id。"""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT run_id FROM runs
                 WHERE status IN ('passed', 'failed', 'error', 'cancelled')
                   AND finished_at != '' AND finished_at < ?
                 ORDER BY run_id
                """,
                (cutoff,),
            ).fetchall()
            run_ids = [str(row["run_id"]) for row in rows]
            if run_ids:
                db.executemany("DELETE FROM runs WHERE run_id = ?", [(item,) for item in run_ids])
        return run_ids

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in (
            "uploads_json",
            "options_json",
            "cases_json",
            "summary_json",
            "evidence_json",
            "case_details_json",
        ):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        first_failure_json = result.pop("first_failure_json")
        result["first_failure"] = json.loads(first_failure_json) if first_failure_json else None
        return result
