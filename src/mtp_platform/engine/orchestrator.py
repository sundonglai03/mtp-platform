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
- **失败有截图**：Playwright 相关步骤失败时自动补一张截图。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Callable

from mtp_contracts.adapters import ActionResult, StepContext
from .assertions import AssertionEngine, AssertionResult
from mtp_contracts.action_catalog import validate_args
from mtp_contracts.case_validator import load_case, require_valid
from mtp_contracts.errors import (
    CancelledError_,
    ConfigError,
    MtpError,
    TimeoutError_,
    classify_exception,
)
from .evidence import EvidenceStore
from mtp_contracts.config import PlatformConfig
from mtp_contracts.redaction import collect_secret_values
from .ports import ToolRegistry
from mtp_contracts.results import CaseResult, RunState, StepResult, StepStatus, new_run_id, now_iso
from mtp_contracts.variables import LazySecrets, resolve

# 主流程阶段。注意 **postconditions 不在这里** ——
# 它由 `_run_cleanup` 在 finally 里执行，保证成功/失败/取消/异常四种情况都跑到，
# 放进来会变成执行两次。
PHASE_ORDER = ("fixtures", "preconditions", "steps")


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
    ) -> None:
        if registry is None:
            raise ConfigError(
                "TestRunner 需要注入 ToolRegistry（具体工具实现由 platform 提供）"
            )
        self.config = config
        self.registry = registry
        self.allow_write = allow_write
        self.artifacts_root = Path(artifacts_root or config.artifact_root())
        self.step_timeout_grace_sec = step_timeout_grace_sec
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mtp-step")
        # Playwright 的同步 API 通过 greenlet 工作；浏览器会话从创建到关闭必须
        # 始终停留在同一 OS 线程，不能与普通工具共用可切换 worker 的线程池。
        self._playwright_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mtp-playwright"
        )
        self._closed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.registry.is_active("playwright"):
            try:
                adapter = self.registry.get("playwright")
                self._playwright_executor.submit(adapter.close).result(timeout=10)
            except Exception:  # noqa: BLE001 - 关闭失败不能阻断其他资源释放
                pass
        self._playwright_executor.shutdown(wait=False, cancel_futures=True)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.registry.close_all()

    def _executor_for(self, adapter_name: str) -> ThreadPoolExecutor:
        return self._playwright_executor if adapter_name == "playwright" else self._executor

    def _invoke_adapter(
        self, adapter_name: str, operation: Callable[..., ActionResult], *args: Any
    ) -> ActionResult:
        """让同步 Playwright 的所有入口保持线程亲和性。"""
        if adapter_name == "playwright":
            return self._playwright_executor.submit(operation, *args).result()
        return operation(*args)

    # ------------------------------------------------------------------
    # 用例级会话隔离
    # ------------------------------------------------------------------
    def _isolate_sessions(self, case: dict[str, Any], result: CaseResult) -> None:
        """让每个用例从「陌生访客」开始，不继承上一个用例的会话。

        背景：一次任务里所有用例**共用同一个浏览器**（会话常驻进程）。用例 A 过了登录
        门禁之后，用例 B 的 `open-web` 会直接落到已登录页面 —— B 里「处理登录/UKey」那
        段路径根本没被检验（假通过），而且 B 单独跑时行为就变了（顺序依赖）。

        规则：
        - 默认开启；部署侧可用 `runner.isolate_case_session: false` 整体关掉；
        - 用例写 `reuse_session: true` 时跳过（它明确要沿用上一个用例的会话）；
        - 只作用于**声明了 `reset_session` 的工具**（目前是浏览器）：ssh / mysql 的
          连接不携带跨用例的身份语义，重建反而白白多花时间。
        """
        if not bool(self.config.runner_default("isolate_case_session", True)):
            return
        if bool(case.get("reuse_session")):
            result.warnings.append(
                "用例声明 reuse_session: true，沿用上一个用例的会话（本用例未做隔离）"
            )
            return

        for name in self._active_adapter_names():
            try:
                adapter = self.registry.get(name)
            except MtpError:
                continue
            reset = getattr(adapter, "reset_session", None)
            if not callable(reset):
                continue
            try:
                self._invoke_adapter(name, reset)
            except Exception as exc:  # noqa: BLE001 - 隔离失败只告警，不能拦住用例
                result.warnings.append(f"{name} 会话隔离失败: {exc}")

    def _declared_actions(self) -> list[str]:
        """已实例化工具**自己声明的**动作（`工具名.动作`）。

        共用目录覆盖平台自带工具；这里补上注入的测试替身与二次开发的自定义工具 ——
        否则在没有真实工具的环境里（单测、本地二次开发），引擎会因为「未知动作」直接
        拒绝用例。真实部署下两者一致，等于没有额外放行。
        """
        declared: list[str] = []
        for name in self._active_adapter_names():
            try:
                adapter = self.registry.get(name)
            except MtpError:
                continue
            actions = getattr(adapter, "actions", None)
            if not callable(actions):
                continue
            try:
                declared.extend(f"{name}.{action}" for action in actions())
            except Exception:  # noqa: BLE001 - 工具自述失败不该拦住用例
                continue
        return declared

    def _active_adapter_names(self) -> list[str]:
        """注册表里已实例化的工具名；注册表没实现该能力时按「无」处理。"""
        names = getattr(self.registry, "active_names", None)
        if not callable(names):
            return []
        try:
            return list(names())
        except Exception:  # noqa: BLE001 - 探测失败不该影响用例执行
            return []

    def __enter__(self) -> "TestRunner":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 单用例执行
    # ------------------------------------------------------------------
    def run_case_file(
        self,
        path: str | Path,
        *,
        run_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CaseResult:
        rid = run_id or new_run_id()
        active_cancel_event = cancel_event or threading.Event()
        store = EvidenceStore(
            self.artifacts_root,
            rid,
            redact_keys=self.config.redact_keys(),
            placeholder=self.config.redact_placeholder(),
        )
        try:
            case = load_case(path)
            require_valid(case, source=str(path), extra_actions=self._declared_actions())
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

        return self.run_case(case, run_id=rid, store=store, cancel_event=active_cancel_event)

    def run_case(
        self,
        case: dict[str, Any],
        *,
        run_id: str,
        store: EvidenceStore,
        cancel_event: threading.Event,
    ) -> CaseResult:
        started = time.monotonic()
        _register_case_credentials(case, store)
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
            self._isolate_sessions(case, result)
            for phase in PHASE_ORDER:
                steps = list(case.get(phase) or [])
                if phase == "fixtures":
                    steps = self._expand_fixtures(steps)
                self._run_phase(phase, steps, context, result, store, cancel_event, case, run_id, acquired)

                if phase == "fixtures":
                    result.warnings.extend(self._audit_fixtures(case))

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
                outcome = self._invoke_adapter(
                    adapter_name, adapter.do_execute, act, args, step_ctx
                )
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
        from .data_manager import FixturePlanner

        try:
            return FixturePlanner().issues(case)
        except Exception:  # noqa: BLE001 - 体检本身不该影响执行
            return []

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

            # 模板解析后按**同一份动作契约**再校一次，闭合校验链路：静态校验时整串
            # `{{ ... }}` 会跳过类型检查（解析后是什么类型由上下文决定），所以变量解析
            # 完必须补这一刀。否则类型不对会一路带进执行阶段，报出来的是底层工具的怪错，
            # 而不是「你引用的变量内容不对」。
            resolved_issues = validate_args(action, args)
            if resolved_issues:
                detail = "；".join(f"{issue.path}: {issue.message}" for issue in resolved_issues)
                record.status = StepStatus.FAILED
                record.error = {
                    "code": "invalid_resolved_arg",
                    "message": f"变量解析后的参数不符合动作契约：{detail}",
                    "detail": "用例里的 {{ ... }} 解析出来的类型或取值不对，请检查被引用的变量内容",
                }
                record.data = {"error": record.error}
                record.summary = str(record.error["message"])
                break

            adapter_name, _, act = action.partition(".")
            # 适配器内部还有一层超时（ssh/mysql 的 args.timeout 是秒、playwright 是毫秒）。
            # 用例写 `timeout: 240` 表达的是「这一步我要等这么久」，看门狗必须让到那之后；
            # 否则轮询步骤会被 30s 默认值先砍掉，报出来的是「步骤超时（30.0s）」，
            # 完全指不到真正原因（实测 cert-05/08/09 就是这么被误杀的）。
            timeout_sec = max(timeout_sec, self._adapter_declared_timeout(adapter_name, act, args))
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

    # 适配器超时与看门狗之间的余量：适配器自己超时后还要收尾（关闭 channel、
    # 组装错误），贴太紧会被看门狗抢在前面，报成「步骤超时」而不是工具自己的报错。
    _ADAPTER_TIMEOUT_MARGIN_SEC = 5.0

    def _adapter_declared_timeout(self, adapter_name: str, action: str, args: dict[str, Any]) -> float:
        """适配器声明的内部超时（秒）；探测不到就按 0 处理（等价于旧行为）。

        只认**显式实现**了 `declared_timeout_sec` 的工具：老工具与测试替身不必实现。
        """
        try:
            adapter = self.registry.get(adapter_name)
        except MtpError:
            return 0.0
        declared = getattr(adapter, "declared_timeout_sec", None)
        if not callable(declared):
            return 0.0
        try:
            value = declared(action, args)
            if value is None:
                return 0.0
            return max(0.0, float(value)) + self._ADAPTER_TIMEOUT_MARGIN_SEC
        except Exception:  # noqa: BLE001 - 探测失败不该影响用例执行
            return 0.0

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

        future = self._executor_for(adapter_name).submit(
            adapter.execute, action, args, step_ctx
        )
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
    # 证据：截图与文本快照
    # ------------------------------------------------------------------
    def _collect_evidence(
        self,
        step: dict[str, Any],
        record: StepResult,
        store: EvidenceStore,
        case: dict[str, Any],
        run_id: str,
    ) -> None:
        """按声明采集浏览器截图或文本快照。

        - `screenshot` 保存 PNG；`snapshot` 保存页面标题、URL 与可见文本；
        - ssh / mysql / http 这类截不了图的步骤不再落 stdout/stderr 文本，
          步骤产出在「步骤输出」里仍然可见，只是不再另存证据文件。
        """
        if not record.action.startswith("playwright."):
            return
        requested = {str(item) for item in (step.get("evidence") or [])}
        if "screenshot" in requested:
            self._screenshot(store, case, run_id, record.step_id, "screenshot")
        if "snapshot" in requested:
            self._snapshot(store, case, run_id, record.step_id, "snapshot")

    def _capture_failure(
        self, record: StepResult, store: EvidenceStore, case: dict[str, Any], run_id: str
    ) -> None:
        """失败自动补一张截图。

        只在**浏览器相关**步骤失败、且浏览器会话已经起过时才去采集 —— 否则一个
        纯 SQL 用例失败会把 Playwright 也拉起来，白白多花十几秒。拿不到截图
        只记一条告警，不掩盖原始错误。
        """
        if not record.action.startswith("playwright."):
            return
        if not self.registry.is_active("playwright"):
            return

        errors: list[str] = []
        try:
            self._screenshot(store, case, run_id, record.step_id, f"failure-{record.step_id}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"截图: {exc}")
        try:
            self._snapshot(store, case, run_id, record.step_id, f"failure-{record.step_id}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"快照: {exc}")
        if errors:
            record.data.setdefault("evidence_warning", f"无法采集失败证据: {'；'.join(errors)}")

    def _screenshot(
        self,
        store: EvidenceStore,
        case: dict[str, Any],
        run_id: str,
        step_id: str,
        name: str,
    ) -> None:
        """截一张整页 PNG 存成证据（真实适配器把 base64 放在 raw 里）。"""
        case_id = str(case.get("id"))
        adapter = self.registry.get("playwright")
        ctx = StepContext(
            run_id=run_id,
            case_id=case_id,
            step_id=step_id,
            env=dict(case.get("environment") or {}),
        )
        outcome = self._invoke_adapter(
            "playwright", adapter.do_execute, "screenshot", {"scale": "css", "fullPage": True}, ctx
        )
        if not outcome.ok:
            raise RuntimeError(outcome.summary)

        for item in outcome.raw:
            if isinstance(item, dict) and item.get("type") == "image" and item.get("_base64"):
                store.save_base64("screenshot", case_id, step_id, name, item["_base64"], ext=".png")
                return

    def _snapshot(
        self,
        store: EvidenceStore,
        case: dict[str, Any],
        run_id: str,
        step_id: str,
        name: str,
    ) -> None:
        """把 Playwright snapshot 的可读文本保存为证据。"""
        case_id = str(case.get("id"))
        adapter = self.registry.get("playwright")
        ctx = StepContext(
            run_id=run_id,
            case_id=case_id,
            step_id=step_id,
            env=dict(case.get("environment") or {}),
        )
        outcome = self._invoke_adapter(
            "playwright", adapter.do_execute, "snapshot", {}, ctx
        )
        if not outcome.ok:
            raise RuntimeError(outcome.summary)
        text = str(outcome.data.get("text") or outcome.data.get("page_text") or "")
        store.save_text(
            "snapshot", case_id, step_id, name, text, ext=".txt", summary="页面文本快照"
        )

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


def _register_case_credentials(case: dict[str, Any], store: EvidenceStore) -> None:
    """登记 JSON 套件中的凭证值，防止进入错误、断言或证据文本。

    取值逻辑放在 core（`collect_secret_values`），与任务结果写入点（`jobs._scrubber`）
    用同一套规则，避免两处各写一遍、其中一处漏掉。
    """
    store.registry.register_many(collect_secret_values(case))
