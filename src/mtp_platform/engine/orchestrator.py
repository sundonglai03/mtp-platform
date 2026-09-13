"""测试任务编排器。

一次 case 的执行顺序（`orchestrator/TASK`）::

    fixtures ─▶ preconditions ─▶ steps ─▶ assertions ─▶ postconditions ─▶ fixture 清理

关键保证：

- **清理必定执行**：postconditions 与 fixture 清理放在 `finally` 里，
  成功 / 失败 / 取消 / 平台异常四种情况都会走；
- **单步超时**：每步按 `timeout_sec` 由看门狗线程兜底（适配器内部还有一层
  MCP/HTTP 超时，通常先触发那个）；
- **重试**：`retry` + `retry_delay_ms`，且每一步的尝试次数都记在结果里；
- **取消**：`cancel(run_id)` 置位后，在步骤之间与重试之间尽快停下，状态落到
  `cancelled`，已经跑过的步骤与证据全部保留；
- **失败有截图**：Playwright 相关步骤失败时自动补一张截图 + 快照，
  并记录当前 URL 与页面文本。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Callable

from ..contracts.adapters import ActionResult, StepContext
from .assertions import AssertionEngine, AssertionResult
from ..contracts.case_validator import load_case, require_valid
from ..contracts.errors import (
    CancelledError_,
    ConfigError,
    MtpError,
    TimeoutError_,
    classify_exception,
)
from .evidence import EvidenceStore
from ..contracts.config import PlatformConfig
from ..contracts.redaction import SecretRegistry
from .ports import ToolRegistry
from ..contracts.results import CaseResult, RunState, StepResult, StepStatus, new_run_id, now_iso
from ..contracts.variables import LazySecrets, resolve

# 主流程阶段。注意 **postconditions 不在这里** ——
# 它由 `_run_cleanup` 在 finally 里执行，保证成功/失败/取消/异常四种情况都跑到，
# 放进来会变成执行两次。
PHASE_ORDER = ("fixtures", "preconditions", "steps")


class _RunHandle:
    """一次 submit 的句柄：状态可查、可取消。"""

    def __init__(self, run_id: str, cases: list[Path]) -> None:
        self.run_id = run_id
        self.cases = cases
        self.state = RunState.QUEUED
        self.cancel_event = threading.Event()
        self.results: list[CaseResult] = []
        self.started_at = ""
        self.finished_at = ""
        self.error: str | None = None

    def to_dict(self, *, include_results: bool = True) -> dict[str, Any]:
        passed = sum(1 for r in self.results if r.status == RunState.PASSED)
        failed = sum(1 for r in self.results if r.status == RunState.FAILED)
        errored = sum(1 for r in self.results if r.status == RunState.ERROR)
        cancelled = sum(1 for r in self.results if r.status == RunState.CANCELLED)
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "state": self.state.value,
            "cases_total": len(self.cases),
            "cases_done": len(self.results),
            "summary": {
                "passed": passed,
                "failed": failed,
                "error": errored,
                "cancelled": cancelled,
            },
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
        if include_results:
            payload["results"] = [r.to_dict() for r in self.results]
        return payload


class TestRunner:
    def __init__(
        self,
        config: PlatformConfig,
        *,
        registry: ToolRegistry | None = None,
        allow_write: bool = False,
        artifacts_root: str | Path | None = None,
        step_timeout_grace_sec: float = 1.0,
        max_workers: int = 4,
        audit: Any = None,
    ) -> None:
        if registry is None:
            raise ConfigError(
                "TestRunner 需要注入 ToolRegistry（具体工具实现由 platform 提供）"
            )
        self.config = config
        self.registry = registry
        self.allow_write = allow_write
        self.audit = audit
        self.artifacts_root = Path(artifacts_root or config.artifact_root())
        self.step_timeout_grace_sec = step_timeout_grace_sec
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mtp-step")
        self._runs: dict[str, _RunHandle] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.registry.close_all()

    def __enter__(self) -> "TestRunner":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 提交 / 查询 / 取消
    # ------------------------------------------------------------------
    def submit(self, case_paths: list[str | Path]) -> str:
        paths = [Path(p) for p in case_paths]
        run_id = new_run_id()
        handle = _RunHandle(run_id, paths)
        with self._lock:
            self._runs[run_id] = handle
        threading.Thread(
            target=self._execute_batch, args=(handle,), name=f"mtp-run-{run_id}", daemon=True
        ).start()
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        handle = self._runs.get(run_id)
        if handle is None:
            raise ConfigError(f"未知 run_id: {run_id}", detail="用 list_runs() 查看已知任务")
        return handle.to_dict()

    def list_runs(self) -> list[dict[str, Any]]:
        return [h.to_dict(include_results=False) for h in self._runs.values()]

    def cancel(self, run_id: str) -> bool:
        handle = self._runs.get(run_id)
        if handle is None or handle.state.terminal:
            return False
        handle.cancel_event.set()
        return True

    def _execute_batch(self, handle: _RunHandle) -> None:
        handle.state = RunState.RUNNING
        handle.started_at = now_iso()
        try:
            for path in handle.cases:
                if handle.cancel_event.is_set():
                    break
                result = self.run_case_file(path, run_id=handle.run_id, handle=handle)
                handle.results.append(result)
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            handle.error = f"{type(exc).__name__}: {exc}"
            handle.state = RunState.ERROR
        finally:
            if handle.state != RunState.ERROR:
                handle.state = self._aggregate_state(handle)
            handle.finished_at = now_iso()

    @staticmethod
    def _aggregate_state(handle: _RunHandle) -> RunState:
        if handle.cancel_event.is_set():
            return RunState.CANCELLED
        states = {r.status for r in handle.results}
        if not states:
            return RunState.CANCELLED
        if RunState.ERROR in states:
            return RunState.ERROR
        if RunState.FAILED in states:
            return RunState.FAILED
        if states == {RunState.PASSED}:
            return RunState.PASSED
        return RunState.CANCELLED

    # ------------------------------------------------------------------
    # 单用例执行
    # ------------------------------------------------------------------
    def run_case_file(
        self, path: str | Path, *, run_id: str | None = None, handle: _RunHandle | None = None
    ) -> CaseResult:
        rid = run_id or new_run_id()
        cancel_event = handle.cancel_event if handle else threading.Event()
        store = EvidenceStore(
            self.artifacts_root,
            rid,
            inline_max_bytes=int(self.config.evidence.get("inline_max_bytes", 8192)),
            redact_keys=self.config.redact_keys(),
            placeholder=self.config.redact_placeholder(),
        )
        try:
            case = load_case(path)
            require_valid(case, source=str(path))
        except MtpError as exc:
            result = CaseResult(
                run_id=rid,
                case_id=Path(path).stem,
                title="(未通过校验)",
                source=str(path),
                status=RunState.ERROR,
                started_at=now_iso(),
                finished_at=now_iso(),
                error=exc.to_dict(),
            )
            return result

        return self.run_case(case, run_id=rid, store=store, cancel_event=cancel_event)

    def run_case(
        self,
        case: dict[str, Any],
        *,
        run_id: str,
        store: EvidenceStore,
        cancel_event: threading.Event,
    ) -> CaseResult:
        started = time.monotonic()
        result = CaseResult(
            run_id=run_id,
            case_id=str(case.get("id")),
            title=str(case.get("title", "")),
            module=str(case.get("module", "")),
            priority=str(case.get("priority", "")),
            tags=list(case.get("tags") or []),
            source=str(case.get("_source", "")),
            status=RunState.RUNNING,
            started_at=now_iso(),
        )

        context = self._build_context(case, run_id)
        engine = self._build_engine(store, case, run_id)
        acquired: list[str] = []

        try:
            for phase in PHASE_ORDER:
                steps = list(case.get(phase) or [])
                if phase == "fixtures":
                    steps = self._expand_fixtures(steps)
                self._run_phase(phase, steps, context, result, store, cancel_event, case, run_id, acquired)

                if phase == "fixtures":
                    # 已写入的数据登记台账：进程被杀/任务被取消后仍能补清理
                    result.warnings.extend(self._audit_fixtures(case))
                    try:
                        self._record_fixtures(run_id, case, result.steps)
                    except Exception as exc:  # noqa: BLE001 - 台账失败不该影响用例
                        result.warnings.append(f"清理台账写入失败: {exc}")

            if not cancel_event.is_set():
                assertions = list(case.get("assertions") or [])
                self._run_assertions(assertions, context, engine, result, store, run_id)

            result.status = self._decide_status(result, cancel_event)

        except CancelledError_:
            result.status = RunState.CANCELLED
        except MtpError as exc:
            result.status = RunState.ERROR
            result.error = exc.to_dict()
        except Exception as exc:  # noqa: BLE001
            result.status = RunState.ERROR
            result.error = classify_exception(exc).to_dict()
        finally:
            # 无论成功、失败还是取消，清理都必须执行
            if cancel_event.is_set() and result.status == RunState.RUNNING:
                result.status = RunState.CANCELLED
            self._run_cleanup(context, result, store, case, run_id)

        result.finished_at = now_iso()
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.evidence = store.to_list()
        return result

    # ------------------------------------------------------------------
    # 上下文与引擎
    # ------------------------------------------------------------------
    def _build_context(self, case: dict[str, Any], run_id: str) -> dict[str, Any]:
        env = dict(case.get("environment") or {})
        variables = dict(case.get("variables") or {})
        context: dict[str, Any] = {
            "env": env,
            "vars": variables,
            "secrets": LazySecrets(case.get("secrets")),
            "steps": {},
            "run": {"id": run_id, "case_id": str(case.get("id", ""))},
            "now": {"iso": now_iso(), "epoch": int(time.time())},
        }
        return context

    def _build_engine(
        self, store: EvidenceStore, case: dict[str, Any], run_id: str
    ) -> AssertionEngine:
        def probe(action: str, args: dict[str, Any]) -> dict[str, Any]:
            adapter_name, _, act = action.partition(".")
            step_ctx = StepContext(
                run_id=run_id,
                case_id=str(case.get("id", "")),
                step_id="(probe)",
                allow_write=False,
                env=dict(case.get("environment") or {}),
                variables=dict(case.get("variables") or {}),
            )
            try:
                adapter = self.registry.get(adapter_name)
                outcome: ActionResult = adapter.do_execute(act, args, step_ctx)
                return {
                    "ok": outcome.ok,
                    "data": outcome.data,
                    "error": outcome.error.to_dict() if outcome.error else None,
                }
            except Exception as exc:  # noqa: BLE001 - 探针失败不该炸掉断言
                return {"ok": False, "data": {}, "error": {"code": "probe_failed", "message": str(exc)}}

        return AssertionEngine(
            probe=probe,
            registry=store.registry,
            redact_keys=self.config.redact_keys(),
            placeholder=self.config.redact_placeholder(),
        )

    # ------------------------------------------------------------------
    # 步骤执行
    # ------------------------------------------------------------------
    def _expand_fixtures(self, fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """fixture 复用 step 的执行形状，额外字段（cleanup/verify）留在原始 dict 里。"""
        return [dict(f) for f in fixtures]

    def _audit_fixtures(self, case: dict[str, Any]) -> list[str]:
        """fixture 体检结果转成用例告警（不阻断执行）。"""
        from .data_manager import DataManager

        try:
            return DataManager(self.config).audit(case)
        except Exception:  # noqa: BLE001 - 体检本身不该影响执行
            return []

    def _record_fixtures(self, run_id: str, case: dict[str, Any], steps: list[StepResult]) -> None:
        from .data_manager import DataManager

        DataManager(self.config).record_applied(run_id, case, steps)

    def _run_phase(
        self,
        phase: str,
        steps: list[dict[str, Any]],
        context: dict[str, Any],
        result: CaseResult,
        store: EvidenceStore,
        cancel_event: threading.Event,
        case: dict[str, Any],
        run_id: str,
        acquired: list[str],
    ) -> None:
        aborted = False
        for index, step in enumerate(steps):
            if cancel_event.is_set():
                # 不抛异常：把剩余步骤标成 cancelled 记进结果，报告里能看到「本来还要跑什么」
                result.steps.append(
                    StepResult(
                        step_id=str(step.get("id")),
                        action=str(step.get("action")),
                        phase=phase,
                        index=index,
                        status=StepStatus.CANCELLED,
                        summary="任务被取消",
                    )
                )
                continue

            if aborted:
                result.steps.append(
                    StepResult(
                        step_id=str(step.get("id")),
                        action=str(step.get("action")),
                        phase=phase,
                        index=index,
                        status=StepStatus.SKIPPED,
                        summary="前序步骤失败，按 abort 策略跳过",
                    )
                )
                continue

            step_result = self._execute_step(step, phase, index, context, store, cancel_event, case, run_id)
            result.steps.append(step_result)
            context["steps"][step_result.step_id] = dict(step_result.data)

            # fixture 的清理注册（倒序执行）
            if phase == "fixtures" and step.get("cleanup"):
                acquired.append(step_result.step_id)

            # fixture 自带的 verify 断言
            if phase == "fixtures" and step.get("verify") and step_result.ok:
                engine = self._build_engine(store, case, run_id)
                self._run_assertions(
                    list(step.get("verify") or []), context, engine, result, store, run_id,
                    id_prefix=f"{step_result.step_id}.",
                )

            if step_result.status in {StepStatus.FAILED, StepStatus.ERROR}:
                policy = str(step.get("on_failure") or case.get("on_failure") or "abort")
                if step.get("continue_on_error"):
                    policy = "continue"
                if policy == "abort":
                    aborted = True
                    result.warnings.append(
                        f"步骤 {step_result.step_id} 失败，后续 {phase} 步骤按 abort 策略跳过"
                    )

    def _execute_step(
        self,
        step: dict[str, Any],
        phase: str,
        index: int,
        context: dict[str, Any],
        store: EvidenceStore,
        cancel_event: threading.Event,
        case: dict[str, Any],
        run_id: str,
    ) -> StepResult:
        step_id = str(step.get("id"))
        action = str(step.get("action", ""))
        record = StepResult(step_id=step_id, action=action, phase=phase, index=index)
        record.started_at = now_iso()

        max_attempts = int(step.get("retry", case.get("retry", 0)) or 0) + 1
        max_attempts = min(max_attempts, int(self.config.runner_default("max_retry", 5)) + 1)
        delay_ms = int(step.get("retry_delay_ms", 0) or 0)
        timeout_sec = float(
            step.get("timeout_sec")
            or case.get("timeout_sec")
            or self.config.runner_default("default_step_timeout_sec", 30)
        )

        started = time.monotonic()
        for attempt in range(1, max_attempts + 1):
            if cancel_event.is_set():
                record.status = StepStatus.CANCELLED
                record.summary = "任务被取消"
                break

            record.attempts = attempt
            try:
                args = resolve(step.get("args") or {}, context, path=f"{phase}[{index}].args")
            except Exception as exc:  # noqa: BLE001
                error = classify_exception(exc, action=action)
                record.status = StepStatus.ERROR
                record.error = error.to_dict()
                record.data = {"error": error.to_dict()}
                record.summary = error.message
                break

            adapter_name, _, act = action.partition(".")
            step_ctx = StepContext(
                run_id=run_id,
                case_id=str(case.get("id", "")),
                step_id=step_id,
                allow_write=self.allow_write,
                env=dict(context.get("env") or {}),
                variables=dict(context.get("vars") or {}),
            )

            outcome = self._call_adapter(adapter_name, act, args, step_ctx, timeout_sec, record)
            record.data = dict(outcome.data)
            record.summary = outcome.summary

            # 审计：每次工具调用都留痕（操作者/时间/环境在 AuditLog 里补齐）
            if self.audit is not None:
                try:
                    self.audit.tool_call(
                        run_id=run_id,
                        case_id=str(case.get("id", "")),
                        step_id=step_id,
                        adapter=adapter_name,
                        action=act,
                        ok=outcome.ok,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    if not outcome.ok and outcome.error is not None and outcome.error.code == "policy_denied":
                        self.audit.policy_denied(
                            run_id=run_id,
                            case_id=str(case.get("id", "")),
                            step_id=step_id,
                            code=outcome.error.code,
                            message=outcome.error.message,
                        )
                except Exception:  # noqa: BLE001 - 审计失败不影响用例
                    pass

            if outcome.ok:
                record.status = StepStatus.PASSED
                break

            error = outcome.error
            record.error = error.to_dict() if error else {"code": "tool_failed", "message": "步骤失败"}
            record.data["error"] = record.error
            record.status = StepStatus.FAILED
            if attempt < max_attempts:
                if delay_ms:
                    time.sleep(delay_ms / 1000)
                continue
            break

        if not record.ok and record.error is None and record.status != StepStatus.CANCELLED:
            record.status = StepStatus.FAILED
            record.error = {"code": "tool_failed", "message": record.summary or "步骤失败"}
            record.data.setdefault("error", record.error)

        # 标准字段：让断言能稳定地写 {{ steps.x.step_ok }} / {{ steps.x.step_status }}；
        # 注意不要覆盖适配器自己的 ok（api 适配器的 ok 表示 HTTP 是否成功）
        record.data.setdefault("step_ok", record.ok)
        record.data["step_status"] = record.status.value
        record.data.setdefault("step_id", step_id)

        # 负向用例：该步骤**本就应该失败**（例如验证策略拦截）。
        # 真的失败了 → 视为通过；反而成功了 → 用例失败。
        if bool(step.get("expect_failure")):
            if record.status in {StepStatus.FAILED, StepStatus.ERROR}:
                record.summary = f"（预期失败）{record.summary}"
                record.status = StepStatus.PASSED
                record.data["step_status"] = record.status.value
                record.data["expected_failure"] = True
            elif record.status == StepStatus.PASSED:
                record.status = StepStatus.FAILED
                record.data["step_status"] = record.status.value
                record.data["expected_failure"] = False
                record.error = {
                    "code": "unexpected_success",
                    "message": "该步骤声明了 expect_failure，但实际成功了",
                }
                record.data["error"] = record.error
                record.data["step_ok"] = False
                record.summary = "预期失败但实际成功"
        record.data.setdefault("step_ok", record.ok)

        record.finished_at = now_iso()
        record.duration_ms = int((time.monotonic() - started) * 1000)

        # 证据：声明式采集 + 失败自动截图
        self._collect_evidence(step, record, store, case, run_id)
        if record.status in {StepStatus.FAILED, StepStatus.ERROR}:
            self._capture_failure(record, store, case, run_id)

        record.evidence = [r.to_dict() for r in store.refs_for(case_id=str(case.get("id")), step_id=step_id)]
        return record

    def _call_adapter(
        self,
        adapter_name: str,
        action: str,
        args: dict[str, Any],
        step_ctx: StepContext,
        timeout_sec: float,
        record: StepResult,
    ) -> ActionResult:
        try:
            adapter = self.registry.get(adapter_name)
        except MtpError as exc:
            return ActionResult.failure(action, exc, adapter=adapter_name)

        future = self._executor.submit(adapter.execute, action, args, step_ctx)
        try:
            return future.result(timeout=timeout_sec + self.step_timeout_grace_sec)
        except FuturesTimeout:
            future.cancel()
            return ActionResult.failure(
                action,
                TimeoutError_(
                    f"步骤超时（{timeout_sec}s）: {adapter_name}.{action}",
                    adapter=adapter_name,
                    action=action,
                    detail="底层调用可能仍在进行；该步已按超时处理",
                ),
                adapter=adapter_name,
            )
        except Exception as exc:  # noqa: BLE001
            return ActionResult.failure(
                action, classify_exception(exc, adapter=adapter_name, action=action), adapter=adapter_name
            )

    # ------------------------------------------------------------------
    # 证据
    # ------------------------------------------------------------------
    def _collect_evidence(
        self,
        step: dict[str, Any],
        record: StepResult,
        store: EvidenceStore,
        case: dict[str, Any],
        run_id: str,
    ) -> None:
        declared = list(step.get("evidence") or [])
        if not declared:
            return
        case_id = str(case.get("id"))
        step_id = record.step_id

        if "text" in declared and record.data.get("text"):
            store.save_text("text", case_id, step_id, "step-output", str(record.data["text"]))

        namespace = record.action.partition(".")[0]
        if namespace != "playwright":
            return

        if "snapshot" in declared:
            self._playwright_evidence(store, case, run_id, "snapshot", step_id, "snapshot",
                                      {"filename": f"snapshot-{step_id}.md"}, ext=".md")
        if "screenshot" in declared:
            self._playwright_evidence(store, case, run_id, "screenshot", step_id, "screenshot",
                                      {"scale": "css", "fullPage": True}, ext=".png")
        if "console" in declared:
            self._playwright_evidence(store, case, run_id, "console", step_id, "console",
                                      {"level": "info", "all": True}, ext=".log")
        if "network" in declared:
            self._playwright_evidence(store, case, run_id, "network", step_id, "network",
                                      {"static": False}, ext=".log")

    def _capture_failure(
        self, record: StepResult, store: EvidenceStore, case: dict[str, Any], run_id: str
    ) -> None:
        """失败自动补证据：截图 + 快照。

        只在**浏览器相关**步骤失败、且浏览器会话已经起过时才去采集 —— 否则一个
        纯 SQL 用例失败会把 Playwright 也拉起来，白白多花十几秒。拿不到浏览器
        只记一条告警，不掩盖原始错误。
        """
        if not record.action.startswith("playwright."):
            return
        if not self.registry.is_active("playwright"):
            return

        step_id = record.step_id
        try:
            self._playwright_evidence(store, case, run_id, "screenshot", step_id,
                                      f"failure-{step_id}", {"scale": "css", "fullPage": True}, ext=".png")
            self._playwright_evidence(store, case, run_id, "snapshot", step_id,
                                      f"failure-{step_id}", {}, ext=".md")
        except Exception as exc:  # noqa: BLE001
            record.data.setdefault("evidence_warning", f"无法采集失败截图: {exc}")

    def _playwright_evidence(
        self,
        store: EvidenceStore,
        case: dict[str, Any],
        run_id: str,
        kind: str,
        step_id: str,
        name: str,
        args: dict[str, Any],
        *,
        ext: str,
    ) -> None:
        case_id = str(case.get("id"))
        adapter = self.registry.get("playwright")
        ctx = StepContext(
            run_id=run_id,
            case_id=case_id,
            step_id=step_id,
            env=dict(case.get("environment") or {}),
        )
        action = "screenshot" if kind == "screenshot" else (
            "snapshot" if kind == "snapshot" else
            "console_messages" if kind == "console" else "network_requests"
        )
        outcome = adapter.do_execute(action, args, ctx)
        if not outcome.ok:
            raise RuntimeError(outcome.summary)

        for item in outcome.raw:
            if isinstance(item, dict) and item.get("type") == "image" and item.get("_base64"):
                store.save_base64(kind, case_id, step_id, name, item["_base64"], ext=".png")
                return
        if outcome.data.get("text"):
            store.save_text(kind, case_id, step_id, name, str(outcome.data["text"]), ext=ext)

    # ------------------------------------------------------------------
    # 断言与清理
    # ------------------------------------------------------------------
    def _run_assertions(
        self,
        assertions: list[dict[str, Any]],
        context: dict[str, Any],
        engine: AssertionEngine,
        result: CaseResult,
        store: EvidenceStore,
        run_id: str,
        *,
        id_prefix: str = "",
    ) -> None:
        for index, assertion in enumerate(assertions):
            evaluation: AssertionResult = engine.evaluate(
                assertion, context, path=f"assertions[{index}]"
            )
            payload = evaluation.to_dict()
            if id_prefix:
                payload["id"] = f"{id_prefix}{payload['id']}"
            result.assertions.append(payload)

    def _run_cleanup(
        self,
        context: dict[str, Any],
        result: CaseResult,
        store: EvidenceStore,
        case: dict[str, Any],
        run_id: str,
    ) -> None:
        """postconditions 与 fixture 清理，倒序执行，失败只告警不改变已定状态。"""
        cancel_event = threading.Event()  # 清理阶段必须跑完，不再响应取消

        post = list(case.get("postconditions") or [])
        for index, step in enumerate(post):
            outcome = self._execute_step(
                step, "postconditions", index, context, store, cancel_event, case, run_id
            )
            outcome.status = outcome.status  # 保持原状态
            result.steps.append(outcome)
            context["steps"][outcome.step_id] = dict(outcome.data)
            if not outcome.ok:
                result.warnings.append(f"后置清理 {outcome.step_id} 未成功: {outcome.summary}")

        fixtures = [f for f in (case.get("fixtures") or []) if f.get("cleanup")]
        for index, fixture in enumerate(reversed(fixtures)):
            cleanup = dict(fixture["cleanup"])
            step = {
                "id": f"cleanup:{fixture.get('id')}",
                "action": cleanup.get("action"),
                "args": cleanup.get("args") or {},
                "on_failure": "continue",
                "timeout_sec": cleanup.get("timeout_sec", 30),
            }
            outcome = self._execute_step(
                step, "cleanup", index, context, store, cancel_event, case, run_id
            )
            result.cleanup.append(outcome.to_dict())
            required = bool(cleanup.get("required"))
            if not outcome.ok:
                result.warnings.append(
                    f"fixture 清理失败 {fixture.get('id')}: {outcome.summary}"
                )
                if required:
                    result.status = RunState.FAILED

    @staticmethod
    def _decide_status(result: CaseResult, cancel_event: threading.Event) -> RunState:
        if cancel_event.is_set():
            return RunState.CANCELLED
        failed_step = any(s.status in {StepStatus.FAILED, StepStatus.ERROR, StepStatus.CANCELLED} for s in result.steps)
        failed_assertion = any(not a.get("passed") for a in result.assertions)
        if failed_step or failed_assertion:
            return RunState.FAILED
        return RunState.PASSED
