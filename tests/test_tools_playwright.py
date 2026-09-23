"""Playwright 直连工具测试（不启动真实浏览器）。

用假的 playwright 对象驱动，覆盖引擎依赖的三条契约：
`snapshot.page_text`、`evaluate.json`、`screenshot.raw[]._base64`。
"""

from __future__ import annotations

import types

import pytest

from mtp_contracts.adapters import StepContext
from mtp_contracts.config import PlatformConfig
from mtp_platform.tools import playwright as pw_module
from mtp_platform.tools.playwright import PlaywrightTool

from pathlib import Path


@pytest.fixture
def fake_pw(monkeypatch):
    state = types.SimpleNamespace(pages=[], handlers={})

    class _Response:
        def __init__(self, status):
            self.status = status

    class _Keyboard:
        def __init__(self, page):
            self.page = page

        def press(self, key):
            self.page.pressed.append(key)

    class _Page:
        def __init__(self):
            self.url = "about:blank"
            self.clicks = []
            self.fills = []
            self.hovered = []
            self.selected = []
            self.pressed = []
            self.waited = []
            self.viewport = None
            self.screenshots = 0
            self.closed = False
            self.keyboard = _Keyboard(self)
            state.pages.append(self)

        def on(self, event, handler):
            state.handlers[event] = handler

        def goto(self, url, timeout=None):
            self.url = url
            return _Response(200)

        def go_back(self, timeout=None):
            self.url = "about:blank"

        def click(self, target, timeout=None):
            self.clicks.append((target, timeout))

        def fill(self, target, text, timeout=None):
            self.fills.append((target, text))

        def type(self, target, text, timeout=None):
            self.fills.append((target, text))

        def hover(self, target, timeout=None):
            self.hovered.append(target)

        def select_option(self, target, values, timeout=None):
            self.selected.append((target, values))

        def wait_for_selector(self, target, timeout=None):
            self.waited.append(target)

        def wait_for_timeout(self, ms):
            self.waited.append(ms)

        def set_viewport_size(self, size):
            self.viewport = size

        def title(self):
            return "Demo"

        def inner_text(self, selector):
            return "hello page"

        def evaluate(self, expression):
            return {"visible": True, "expr": expression}

        def screenshot(self, full_page=True):
            self.screenshots += 1
            return b"\x89PNG-fake"

        def close(self):
            self.closed = True

    class _Context:
        def __init__(self):
            self.pages_created = []

        def new_page(self):
            page = _Page()
            self.pages_created.append(page)
            return page

        def close(self):
            pass

    class _Browser:
        def __init__(self):
            self.contexts = []

        def new_context(self, **kwargs):
            ctx = _Context()
            self.contexts.append(ctx)
            return ctx

        def close(self):
            pass

    class _Playwright:
        def __init__(self):
            self.chromium = self

        def launch(self, headless=True):
            state.headless = headless
            return _Browser()

        def stop(self):
            state.stopped = True

    module = types.SimpleNamespace(sync_playwright=lambda: types.SimpleNamespace(start=lambda: _Playwright()))
    monkeypatch.setattr(pw_module, "sync_playwright", module.sync_playwright)
    return state


def _ctx(**kw) -> StepContext:
    return StepContext(**kw)


def _cfg(**security) -> PlatformConfig:
    return PlatformConfig(raw={"security": security}, path=Path("mtp_config.yaml"))


def test_navigate_sets_url_and_status(config, fake_pw):
    result = PlaywrightTool(config).execute("navigate", {"url": "http://x/"}, _ctx())
    assert result.ok, result.error
    assert result.data["http_status"] == 200
    assert fake_pw.pages[-1].url == "http://x/"


def test_click_and_type_use_target_alias(config, fake_pw):
    tool = PlaywrightTool(config)
    tool.execute("navigate", {"url": "http://x/"}, _ctx())
    assert tool.execute("click", {"selector": "#go"}, _ctx()).ok
    assert tool.execute("type", {"target": "#name", "text": "bob"}, _ctx()).ok
    page = fake_pw.pages[-1]
    assert page.clicks == [("#go", 15000)]
    assert page.fills == [("#name", "bob")]


def test_snapshot_exposes_page_text(config, fake_pw):
    """断言引擎的 page_text_contains 依赖 snapshot 的 page_text。"""
    result = PlaywrightTool(config).execute("snapshot", {}, _ctx())
    assert result.ok, result.error
    assert result.data["page_text"] == "hello page"
    assert "hello page" in result.data["text"]


def test_screenshot_emits_base64_for_evidence_layer(config, fake_pw):
    """证据层从 raw[]._base64 落盘图片。"""
    result = PlaywrightTool(config).execute("screenshot", {}, _ctx())
    assert result.ok, result.error
    assert result.raw and result.raw[0]["type"] == "image"
    assert result.raw[0]["_base64"]
    assert "_base64" not in result.data  # 不能留在 data 里


def test_evaluate_requires_allow_js_from_a_case(config, fake_pw):
    result = PlaywrightTool(config).execute("evaluate", {"function": "1+1"}, _ctx())
    assert result.ok is False
    assert result.error.code == "config_error"


def test_evaluate_returns_json_via_do_execute_for_probe(config, fake_pw):
    """平台内部的可见性探针走 do_execute，不受 allow_js 限制。"""
    tool = PlaywrightTool(config)
    tool.execute("navigate", {"url": "http://x/"}, _ctx())
    outcome = tool.do_execute("evaluate", {"function": "() => ({visible:true})"}, _ctx())
    assert outcome.ok
    assert outcome.data["json"]["visible"] is True


def test_console_and_network_are_collected(config, fake_pw):
    tool = PlaywrightTool(config)
    tool.execute("navigate", {"url": "http://x/"}, _ctx())
    page = fake_pw.pages[-1]
    fake_pw.handlers["console"](types.SimpleNamespace(type="error", text="boom"))
    fake_pw.handlers["request"](types.SimpleNamespace(method="GET", url="http://x/api"))

    console = tool.execute("console_messages", {}, _ctx())
    network = tool.execute("network_requests", {}, _ctx())
    assert console.ok and "boom" in console.data["text"]
    assert network.ok and "GET http://x/api" in network.data["text"]
    assert page is not None


def test_unknown_action_is_rejected(config):
    result = PlaywrightTool(config).execute("drag", {"target": "#a"}, _ctx())
    assert result.ok is False
    assert result.error.code == "config_error"


def test_missing_playwright_gives_clear_error(config, monkeypatch):
    monkeypatch.setattr(pw_module, "sync_playwright", None)
    result = PlaywrightTool(config).execute("navigate", {"url": "http://x/"}, _ctx())
    assert result.ok is False
    assert result.error.code == "config_error"
    assert "playwright" in result.error.message


def test_headless_follows_config(config, fake_pw):
    PlaywrightTool(_cfg(playwright_headless=False)).execute("navigate", {"url": "http://x/"}, _ctx())
    assert fake_pw.headless is False


def test_close_releases_session(config, fake_pw):
    tool = PlaywrightTool(config)
    tool.execute("navigate", {"url": "http://x/"}, _ctx())
    tool.close()
    assert fake_pw.stopped is True


# ---------------------------------------------------------------------------
# 两层超时：写对「工具自己的超时」，引擎的看门狗必须让到它之后
# ---------------------------------------------------------------------------
def test_declared_timeout_sec_converts_ms_to_seconds(config):
    """playwright 的 args.timeout 是毫秒，声明给引擎时必须换算成秒。"""
    tool = PlaywrightTool(config)
    assert tool.declared_timeout_sec("click", {}) == 15.0
    assert tool.declared_timeout_sec("click", {"timeout": 45000}) == 45.0
    assert tool.declared_timeout_sec("navigate", {}) == 30.0
    assert tool.declared_timeout_sec("wait_for", {"time": 240}) == 240.0


def test_text_hint_extraction():
    assert pw_module._text_hint("button:has-text('登录')") == "登录"
    assert pw_module._text_hint(':text("跳过")') == "跳过"
    assert pw_module._text_hint("text=证书申请") == "证书申请"
    assert pw_module._text_hint("#username") == ""


def test_click_timeout_reports_what_is_really_on_the_page(config):
    """超时错误必须带上「页面真实 DOM」，否则 agent 与人都只能对着 15s 空等猜。

    这条对应真实事故：登录页的按钮是 `<input type=button value=登录>`，用例写的是
    `button:has-text('登录')`（页面上一个 button 都没有），白等 15s 后报的还是
    `Page.click: Timeout 15000ms exceeded.`——看不出该改成什么。
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    class _Locator:
        def count(self) -> int:
            return 0

    class _Page:
        url = "http://192.168.14.131:22001/isc_sso/login"

        def locator(self, selector):
            return _Locator()

        def evaluate(self, expression, arg):
            return ['input#submi.Submit  "登录"']

        def click(self, selector, timeout=None):
            raise PWTimeout("Page.click: Timeout 15000ms exceeded.")

    tool = PlaywrightTool(config)
    tool._page = _Page()  # 直接给一个页面替身，不启动浏览器

    result = tool.execute("click", {"target": "button:has-text('登录')"}, _ctx())

    assert not result.ok
    assert result.error.code == "timeout"
    assert "input#submi" in result.error.detail
    assert "text=登录" in result.error.detail
