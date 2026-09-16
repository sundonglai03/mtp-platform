"""Playwright 直连工具（官方 playwright Python 包）。

与基线 `playwright_adapter` 的关系：
- **语义 action 名沿用**（navigate / click / snapshot / screenshot / evaluate …），
  用例不用改；
- 传输换成官方 playwright，浏览器会话**常驻本进程**（原来活在 MCP 子进程里），
  因此不再需要解析 `### Result` / `### Error` 文本块 —— 失败就是异常，结果就是返回值。

引擎对本工具的契约（必须满足，否则断言/证据会退化）：
- `snapshot` → `data["page_text"]`（页面可见文本）与 `data["text"]`（可读快照）；
- `evaluate` → `data["json"]`（求值结果，`element_visible` 断言依赖它）；
- `screenshot` → `raw=[{"type": "image", "_base64": ...}]`（证据层据此落盘图片）。

安全：`evaluate` / `run_code_unsafe` 属高危，用例必须显式 `allow_js: true`；
平台内部的可见性探针走 `do_execute`，不受此限。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from mtp_contracts.adapters import ActionResult, StepContext
from mtp_contracts.errors import ConfigError, ToolExecutionError
from .base import BaseTool

try:  # 未安装 playwright 时给出明确提示
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]

# 只列出**已实现**的 action；未实现的宁可直接报错，也不静默当作通过。
_ACTIONS: dict[str, str] = {
    "navigate": "打开 URL",
    "navigate_back": "后退",
    "click": "点击元素",
    "type": "填写/输入文本",
    "fill_form": "批量填写表单",
    "press_key": "按键",
    "hover": "悬停",
    "select_option": "选择下拉项",
    "wait_for": "等待元素/时间",
    "snapshot": "页面快照（文本）",
    "screenshot": "截图",
    "evaluate": "在页面执行 JS（高危）",
    "console_messages": "控制台日志",
    "network_requests": "网络请求",
    "resize": "调整视口",
    "close": "关闭浏览器",
}

_HIGH_RISK_ACTIONS = {"evaluate", "run_code_unsafe"}
_ARG_ALIASES = {"selector": "target", "ref": "target"}

_IMAGE_MAX = 5 * 1024 * 1024


class PlaywrightTool(BaseTool):
    name = "playwright"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._console: list[dict[str, Any]] = []
        self._requests: list[dict[str, Any]] = []

    def actions(self) -> dict[str, str]:
        return dict(_ACTIONS)

    # -- 策略 ---------------------------------------------------------------
    def pre_execute(self, action: str, args: dict[str, Any], context: StepContext) -> None:
        if action in _HIGH_RISK_ACTIONS and not (args or {}).get("allow_js"):
            raise ConfigError(
                f"playwright.{action} 属于高危操作，需要显式 allow_js: true",
                adapter=self.name,
                action=action,
            )

    # -- 会话（常驻本进程）--------------------------------------------------
    def _page_obj(self):
        if self._page is not None:
            return self._page
        if sync_playwright is None:  # pragma: no cover - 依赖缺失路径
            raise ConfigError(
                "未安装 playwright，无法使用 playwright 工具",
                adapter=self.name,
                detail="安装： uv sync --extra playwright && playwright install chromium",
            )
        headless = bool(self.config.security.get("playwright_headless", True))
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._context = self._browser.new_context()
        page = self._context.new_page()
        page.on("console", lambda m: self._console.append({"type": m.type, "text": m.text}))
        page.on("request", lambda r: self._requests.append({"method": r.method, "url": r.url}))
        self._page = page
        return page

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._context = self._page = None

    def health(self) -> bool:
        """playwright 可导入即算健康（不主动起浏览器）。"""
        return sync_playwright is not None

    # -- 执行 ---------------------------------------------------------------
    def do_execute(self, action: str, args: dict[str, Any], context: StepContext) -> ActionResult:
        args = self._normalize(args)
        started = time.monotonic()
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:  # pragma: no cover - execute() 已挡未实现的 action
            raise ToolExecutionError(f"playwright 未实现 action: {action}", adapter=self.name, action=action)

        data = handler(args) or {}
        raw = data.pop("_raw", [])
        elapsed_ms = int((time.monotonic() - started) * 1000)
        data.setdefault("duration_ms", elapsed_ms)
        data.setdefault("page_url", self._safe_url())
        return ActionResult.success(
            action,
            adapter=self.name,
            data=data,
            summary=_summarize(action, data, elapsed_ms),
            raw=raw,
        )

    @staticmethod
    def _normalize(args: dict[str, Any]) -> dict[str, Any]:
        return {_ARG_ALIASES.get(k, k): v for k, v in (args or {}).items() if k != "allow_js"}

    def _safe_url(self) -> str:
        try:
            return self._page.url if self._page is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    # -- 各 action ----------------------------------------------------------
    def _do_navigate(self, args: dict[str, Any]) -> dict[str, Any]:
        url = args.get("url")
        if not url:
            raise ConfigError("playwright navigate 缺少 url", adapter=self.name)
        response = self._page_obj().goto(str(url), timeout=_ms(args, "timeout", 30000))
        status = response.status if response is not None else None
        return {"url": str(url), "http_status": status, "text": f"navigated {url} -> {status}"}

    def _do_navigate_back(self, args: dict[str, Any]) -> dict[str, Any]:
        self._page_obj().go_back(timeout=_ms(args, "timeout", 30000))
        return {"text": f"back -> {self._safe_url()}"}

    def _do_click(self, args: dict[str, Any]) -> dict[str, Any]:
        target = _target(args)
        self._page_obj().click(target, timeout=_ms(args, "timeout", 15000))
        return {"target": target, "text": f"clicked {target}"}

    def _do_hover(self, args: dict[str, Any]) -> dict[str, Any]:
        target = _target(args)
        self._page_obj().hover(target, timeout=_ms(args, "timeout", 15000))
        return {"target": target, "text": f"hovered {target}"}

    def _do_type(self, args: dict[str, Any]) -> dict[str, Any]:
        target = _target(args)
        text = args.get("text", "")
        page = self._page_obj()
        if args.get("slowly"):
            page.type(target, str(text), timeout=_ms(args, "timeout", 15000))
        else:
            page.fill(target, str(text), timeout=_ms(args, "timeout", 15000))
        return {"target": target, "text": f"filled {target}"}

    def _do_fill_form(self, args: dict[str, Any]) -> dict[str, Any]:
        fields = args.get("fields") or {}
        page = self._page_obj()
        for target, value in fields.items():
            page.fill(str(target), str(value), timeout=_ms(args, "timeout", 15000))
        return {"filled": sorted(fields), "text": f"filled {len(fields)} field(s)"}

    def _do_press_key(self, args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("key") or "")
        if not key:
            raise ConfigError("playwright press_key 缺少 key", adapter=self.name)
        self._page_obj().keyboard.press(key)
        return {"key": key, "text": f"pressed {key}"}

    def _do_select_option(self, args: dict[str, Any]) -> dict[str, Any]:
        target = _target(args)
        values = args.get("values", args.get("value"))
        self._page_obj().select_option(target, values, timeout=_ms(args, "timeout", 15000))
        return {"target": target, "text": f"selected on {target}"}

    def _do_wait_for(self, args: dict[str, Any]) -> dict[str, Any]:
        page = self._page_obj()
        if args.get("time") is not None:
            page.wait_for_timeout(int(float(args["time"]) * 1000))
            return {"text": f"waited {args['time']}s"}
        target = args.get("target") or args.get("selector")
        if target:
            page.wait_for_selector(str(target), timeout=_ms(args, "timeout", 15000))
            return {"target": str(target), "text": f"waited for {target}"}
        raise ConfigError("playwright wait_for 需要 target 或 time", adapter=self.name)

    def _do_resize(self, args: dict[str, Any]) -> dict[str, Any]:
        width = int(args.get("width", 1280))
        height = int(args.get("height", 800))
        self._page_obj().set_viewport_size({"width": width, "height": height})
        return {"width": width, "height": height, "text": f"resized {width}x{height}"}

    def _do_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        page = self._page_obj()
        try:
            body_text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            body_text = ""
        title = page.title()
        snapshot = f"# {title}\n\nURL: {page.url}\n\n{body_text[:20000]}"
        return {"page_text": body_text, "title": title, "text": snapshot}

    def _do_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        page = self._page_obj()
        png = page.screenshot(full_page=bool(args.get("fullPage", True)))
        if len(png) > _IMAGE_MAX:
            png = page.screenshot(full_page=False)
        b64 = base64.b64encode(png).decode("ascii")
        return {
            "bytes": len(png),
            # 证据层读 raw 里的 _base64 落盘
            "_raw": [
                {"type": "image", "mimeType": "image/png", "bytes": len(png), "_base64": b64}
            ],
        }

    def _do_evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        expression = args.get("function") or args.get("expression")
        if not expression:
            raise ConfigError("playwright evaluate 缺少 function/expression", adapter=self.name)
        value = self._page_obj().evaluate(str(expression))
        return {"json": value, "text": json.dumps(value, ensure_ascii=False, default=str)}

    def _do_console_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        level = str(args.get("level") or "").lower()
        items = [m for m in self._console if not level or m["type"].lower() == level]
        lines = [f"[{m['type']}] {m['text']}" for m in items]
        return {"messages": items, "text": "\n".join(lines) or "(无控制台输出)"}

    def _do_network_requests(self, args: dict[str, Any]) -> dict[str, Any]:
        lines = [f"{r['method']} {r['url']}" for r in self._requests]
        return {"requests": list(self._requests), "text": "\n".join(lines) or "(无网络请求)"}

    def _do_close(self, args: dict[str, Any]) -> dict[str, Any]:
        self.close()
        return {"text": "browser closed"}


def _target(args: dict[str, Any]) -> str:
    target = args.get("target")
    if not target:
        raise ConfigError("playwright 步骤缺少 target（CSS 选择器）", adapter="playwright")
    return str(target)


def _ms(args: dict[str, Any], key: str, default: int) -> int:
    """步骤参数里的 timeout 一律按**毫秒**解释（与 playwright API 一致）。"""
    value = args.get(key)
    return default if value is None else int(value)


def _summarize(action: str, data: dict[str, Any], elapsed_ms: int) -> str:
    url = data.get("page_url") or ""
    if action == "navigate":
        return f"打开 {data.get('url')} -> {data.get('http_status')}（{elapsed_ms}ms）"
    if action == "screenshot":
        return f"截图 {data.get('bytes', 0)} 字节（{elapsed_ms}ms）"
    return f"{action} @ {url}（{elapsed_ms}ms）"


__all__ = ["PlaywrightTool"]
