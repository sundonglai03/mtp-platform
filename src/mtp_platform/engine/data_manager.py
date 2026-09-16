"""测试数据管理：准备、隔离、回滚、清理。

职责边界：**执行**由编排器完成（fixture 就是带 `cleanup` 的步骤），本模块负责
「格式 + 隔离 + 可追溯的清理」。

三条关键设计：

1. **run 级隔离**：每个 fixture 的统一变量是 `{{ run.id }}`（唯一 `test_run_id`），
   两个并行任务写的数据天然不重叠，清理时也按它精确匹配；
2. **清理台账**：fixture 一旦写入成功，就把「怎么删」记到
   `artifacts/_ledger/<run_id>.json`。进程被 kill、任务被取消、机器断电，
   都能用 `run_tests.py cleanup-run <run_id>` 补一刀 —— 这是「中断任务可以再次
   清理」的实现方式；
3. **库/行数限制**：由 mysql 适配器在调用前拦截（白名单 + 生产库黑名单 +
   `max_affected_rows`）。本模块不再重复实现，但会把它写进 fixture 校验。

注意：写操作整体仍受 `--allow-write` 管控，用例本身无法提权。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtp_contracts.errors import ConfigError, StateCorruptError
from mtp_contracts.config import PlatformConfig

LEDGER_DIRNAME = "_ledger"

# 同一个 ledger 的「读-改-写」必须串行，否则两次 record() 交错会丢条目。
# 只保证**进程内**并发安全；跨进程由「每个 run 一个文件」天然隔离。
_LEDGER_LOCK = threading.Lock()


@dataclass
class FixtureSpec:
    """一个 fixture 的完整生命周期。"""

    fixture_id: str
    setup_action: str
    setup_args: dict[str, Any] = field(default_factory=dict)
    verify: list[dict[str, Any]] = field(default_factory=list)
    cleanup_action: str | None = None
    cleanup_args: dict[str, Any] = field(default_factory=dict)
    cleanup_required: bool = False
    description: str = ""

    def to_cleanup_step(self) -> dict[str, Any] | None:
        if not self.cleanup_action:
            return None
        return {
            "id": f"cleanup:{self.fixture_id}",
            "action": self.cleanup_action,
            "args": dict(self.cleanup_args),
            "on_failure": "continue",
        }


class FixturePlanner:
    """把用例里的 `fixtures` 段解析成可执行的计划，并做静态体检。"""

    def plan(self, case: dict[str, Any]) -> list[FixtureSpec]:
        specs: list[FixtureSpec] = []
        for raw in case.get("fixtures") or []:
            cleanup = raw.get("cleanup") or {}
            specs.append(
                FixtureSpec(
                    fixture_id=str(raw.get("id")),
                    setup_action=str(raw.get("action")),
                    setup_args=dict(raw.get("args") or {}),
                    verify=list(raw.get("verify") or []),
                    cleanup_action=str(cleanup.get("action")) if cleanup.get("action") else None,
                    cleanup_args=dict(cleanup.get("args") or {}),
                    cleanup_required=bool(cleanup.get("required")),
                    description=str(raw.get("description") or ""),
                )
            )
        return specs

    def issues(self, case: dict[str, Any]) -> list[str]:
        """fixture 相关的可疑点（不阻断执行，作为告警返回）。"""
        problems: list[str] = []
        for spec in self.plan(case):
            if not spec.setup_action.startswith(("mysql.", "api.", "ssh.")):
                problems.append(
                    f"fixture {spec.fixture_id}: 动作 {spec.setup_action} 通常不是数据准备动作"
                )
            if spec.setup_action in {"mysql.insert", "mysql.update", "mysql.delete"}:
                if not spec.cleanup_action:
                    problems.append(
                        f"fixture {spec.fixture_id}: 写入了数据但没有 cleanup，"
                        "会污染测试库（建议提供 cleanup，或由用例显式声明这是有意为之）"
                    )
                if not _mentions_run_id(spec.setup_args):
                    problems.append(
                        f"fixture {spec.fixture_id}: 写入的数据没有用到 {{{{ run.id }}}}，"
                        "并行运行时可能互相干扰"
                    )
        return problems


def _mentions_run_id(value: Any) -> bool:
    if isinstance(value, str):
        return "run.id" in value
    if isinstance(value, dict):
        return any(_mentions_run_id(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_mentions_run_id(v) for v in value)
    return False


class CleanupLedger:
    """按 run 记录「已写入的数据怎么删」。

    两条关键保证：

    1. **原子写入**：先写同目录临时文件、`fsync`，再 `os.replace()` 顶替目标。
       `os.replace` 在同一文件系统上是原子的，所以进程被 kill / 机器断电
       都不会留下「半截 JSON」。
    2. **损坏必报**：解析失败抛 `StateCorruptError`，不再静默返回空列表 ——
       静默降级等于悄悄丢掉「中断任务补清理」这个能力，问题会被藏起来。
    """

    def __init__(self, root: str | Path, run_id: str) -> None:
        self.root = Path(root) / LEDGER_DIRNAME
        self.run_id = run_id
        self.path = self.root / f"{run_id}.json"

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_text(encoding="utf-8")
        if not raw.strip():
            # 零字节文件当成空台账：原子写入保证了不会有「写了一半」的文件，
            # 空文件只可能是被外部 `touch` 出来的，不为难使用者。
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateCorruptError(
                f"清理台账损坏: {self.path}",
                detail=(
                    f"第 {exc.lineno} 行附近无法解析（{exc.msg}）。"
                    "台账损坏后无法自动补清理；确认该 run 没有残留数据可以删掉这个文件，"
                    f"否则请按 run_id 手工清理后重新生成"
                ),
            ) from exc
        if not isinstance(parsed, list):
            raise StateCorruptError(
                f"清理台账结构非法: {self.path}",
                detail=f"期望 JSON 数组，实际是 {type(parsed).__name__}",
            )
        return parsed

    def _write(self, entries: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # 临时文件必须与目标同目录：只有同一文件系统上 os.replace 才是原子的
        fd, tmp = tempfile.mkstemp(
            dir=str(self.root), prefix=f".{self.run_id}-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def record(
        self,
        *,
        case_id: str,
        fixture_id: str,
        action: str,
        args: dict[str, Any],
    ) -> None:
        with _LEDGER_LOCK:
            entries = self._read()
            entries.append(
                {
                    "case_id": case_id,
                    "fixture_id": fixture_id,
                    "action": action,
                    "args": args,
                }
            )
            self._write(entries)

    def entries(self) -> list[dict[str, Any]]:
        return self._read()

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class DataManager:
    """对外的门面：计划 + 台账 + 补清理。"""

    def __init__(self, config: PlatformConfig, *, registry=None) -> None:
        self.config = config
        self.registry = registry
        self.planner = FixturePlanner()

    # -- 计划 ---------------------------------------------------------------
    def plan(self, case: dict[str, Any]) -> list[FixtureSpec]:
        return self.planner.plan(case)

    def audit(self, case: dict[str, Any]) -> list[str]:
        return self.planner.issues(case)

    # -- 台账 ---------------------------------------------------------------
    def ledger(self, run_id: str) -> CleanupLedger:
        return CleanupLedger(self.config.artifact_root(), run_id)

    def record_applied(self, run_id: str, case: dict[str, Any], steps: list[Any]) -> int:
        """把执行成功的 fixture 记入台账，供中断后补清理。"""
        ledger = self.ledger(run_id)
        specs = {s.fixture_id: s for s in self.plan(case)}
        recorded = 0
        for step in steps:
            if step.phase != "fixtures" or not step.ok:
                continue
            spec = specs.get(step.step_id)
            if spec is None or not spec.cleanup_action:
                continue
            ledger.record(
                case_id=str(case.get("id")),
                fixture_id=spec.fixture_id,
                action=spec.cleanup_action,
                args=spec.cleanup_args,
            )
            recorded += 1
        return recorded

    # -- 补清理 -------------------------------------------------------------
    def replay_cleanup(self, run_id: str, *, allow_write: bool = False) -> list[dict[str, Any]]:
        """重放某次 run 的清理动作（用于中断的任务）。

        需要 `allow_write=True`：清理也是写操作，不能绕过同意机制。
        """
        from mtp_contracts.adapters import StepContext

        ledger = self.ledger(run_id)
        entries = ledger.entries()
        if not entries:
            return []

        registry = self.registry
        if registry is None:
            raise ConfigError(
                "replay_cleanup 需要注入 ToolRegistry（由 platform 提供具体工具实现）"
            )
        # 注册表由 platform 注入并负责关闭，这里不自建
        owned = False
        results: list[dict[str, Any]] = []
        try:
            # 倒序：后写入的先删，避免外键顺序问题
            for entry in reversed(entries):
                adapter_name, _, action = str(entry["action"]).partition(".")
                outcome = registry.get(adapter_name).execute(
                    action,
                    dict(entry.get("args") or {}),
                    StepContext(run_id=run_id, case_id=str(entry.get("case_id", "")), allow_write=allow_write),
                )
                results.append(
                    {
                        "fixture_id": entry.get("fixture_id"),
                        "action": entry.get("action"),
                        "ok": outcome.ok,
                        "summary": outcome.summary,
                    }
                )
        finally:
            if owned:
                registry.close_all()

        if all(r["ok"] for r in results):
            ledger.clear()
        return results
