"""审计日志。

`security-ci/TASK` 要求记录：**操作者、时间、环境、用例、工具调用**。

用 JSONL 追加写，一行一个事件，方便直接喂给日志系统。
写之前统一脱敏，审计日志自己不能变成泄露渠道。
"""

from __future__ import annotations

import getpass
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mtp_contracts.redaction import DEFAULT_PLACEHOLDER, DEFAULT_REDACT_KEYS, redact

EVENT_RUN_START = "run.start"
EVENT_RUN_END = "run.end"
EVENT_CASE_START = "case.start"
EVENT_CASE_END = "case.end"
EVENT_TOOL_CALL = "tool.call"
EVENT_POLICY_DENIED = "policy.denied"


def operator() -> str:
    """操作者标识：优先显式环境变量，其次系统登录名。"""
    return (
        os.environ.get("MTP_OPERATOR")
        or os.environ.get("GITLAB_USER_LOGIN")
        or os.environ.get("GITHUB_ACTOR")
        or _safe_user()
    )


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - 无 tty/无 passwd 时兜底
        return "unknown"


def environment_info() -> dict[str, Any]:
    return {
        "host": socket.gethostname(),
        "cwd": os.getcwd(),
        "ci": bool(os.environ.get("CI")),
        "ci_name": os.environ.get("GITHUB_ACTIONS") and "github-actions"
        or os.environ.get("GITLAB_CI") and "gitlab-ci"
        or "",
        "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}",
    }


class AuditLog:
    def __init__(
        self,
        path: str | Path,
        *,
        redact_keys: list[str] | None = None,
        placeholder: str = DEFAULT_PLACEHOLDER,
    ) -> None:
        self.path = Path(path)
        self.redact_keys = list(redact_keys or DEFAULT_REDACT_KEYS)
        self.placeholder = placeholder
        self._operator = operator()
        self._env = environment_info()

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "operator": self._operator,
            "env": self._env,
        }
        entry.update(fields)
        safe = redact(
            entry, keys=self.redact_keys, placeholder=self.placeholder
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")
        return safe

    # -- 语义化封装 ---------------------------------------------------------
    def run_start(self, *, run_id: str, cases: list[str], allow_write: bool) -> None:
        self.record(
            EVENT_RUN_START,
            run_id=run_id,
            cases=cases,
            allow_write=allow_write,
            cases_total=len(cases),
        )

    def run_end(self, *, run_id: str, status: str, duration_ms: int, summary: dict[str, Any]) -> None:
        self.record(EVENT_RUN_END, run_id=run_id, status=status, duration_ms=duration_ms, summary=summary)

    def case_end(self, *, run_id: str, case_id: str, status: str, duration_ms: int) -> None:
        self.record(EVENT_CASE_END, run_id=run_id, case_id=case_id, status=status, duration_ms=duration_ms)

    def tool_call(
        self,
        *,
        run_id: str,
        case_id: str,
        step_id: str,
        adapter: str,
        action: str,
        ok: bool,
        duration_ms: int,
    ) -> None:
        self.record(
            EVENT_TOOL_CALL,
            run_id=run_id,
            case_id=case_id,
            step_id=step_id,
            adapter=adapter,
            action=action,
            ok=ok,
            duration_ms=duration_ms,
        )

    def policy_denied(self, *, run_id: str, case_id: str, step_id: str, code: str, message: str) -> None:
        self.record(
            EVENT_POLICY_DENIED,
            run_id=run_id,
            case_id=case_id,
            step_id=step_id,
            code=code,
            message=message,
        )
