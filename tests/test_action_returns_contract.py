"""动作返回字段 vs 动作目录：起桩服务真跑一遍 api.*，比对返回的键集。

为什么先覆盖 api.*：playwright 的返回值来自真浏览器、ssh 要真主机、mysql 要真库，
只有 api 能在测试里用标准库起个桩服务完整跑通 —— 也正好是历史上出过「契约说有、
实现没有」的那一族（api.download 声明了 status_code/url/method，实现只返回 5 个字段，
用例引用会在运行时才炸）。

断言口径：
- 成功路径：返回的键集 == 目录声明（去掉只在失败时出现的 `error`）；
- 失败路径：必须带上 `error`（`allow_error: true` 放行后同样带，便于排查）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from mtp_contracts.action_catalog import spec_for
from mtp_contracts.adapters import StepContext
from mtp_platform.tools.http import ApiTool

PAYLOAD = {"a": {"b": 42}, "items": [1, 2, 3]}
BINARY = b"\x00\x01\x02mtp-binary\xff"


class _StubHandler(BaseHTTPRequestHandler):
    """极简桩服务：/boom 返 500，/bin 返二进制，其余返 JSON。"""

    protocol_version = "HTTP/1.1"

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self) -> None:
        if self.path.startswith("/boom"):
            self._respond(500, json.dumps({"message": "boom"}).encode(), "application/json")
        elif self.path.startswith("/bin"):
            self._respond(200, BINARY, "application/octet-stream")
        else:
            self._respond(200, json.dumps(PAYLOAD).encode(), "application/json")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle

    def log_message(self, *args: Any) -> None:  # 测试输出保持干净
        return None


@pytest.fixture(scope="module")
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _context(step_id: str = "s1") -> StepContext:
    return StepContext(run_id="unit-run", case_id="UNIT-API", step_id=step_id)


def _run(config, action: str, args: dict[str, Any]):
    tool = ApiTool(config)
    try:
        return tool.execute(action, args, _context())
    finally:
        tool.close()


ArgsBuilder = Callable[[str], dict[str, Any]]

CASES: list[tuple[str, ArgsBuilder]] = [
    ("get", lambda base: {"url": f"{base}/json"}),
    ("head", lambda base: {"url": f"{base}/json"}),
    ("options", lambda base: {"url": f"{base}/json"}),
    ("post", lambda base: {"url": f"{base}/json", "json": {"x": 1}}),
    ("put", lambda base: {"url": f"{base}/json", "json": {"x": 1}}),
    ("patch", lambda base: {"url": f"{base}/json", "json": {"x": 1}}),
    ("delete", lambda base: {"url": f"{base}/json"}),
    ("request", lambda base: {"url": f"{base}/json", "method": "GET"}),
    ("download", lambda base: {"url": f"{base}/bin"}),
]


@pytest.mark.parametrize("action,build_args", CASES, ids=[case[0] for case in CASES])
def test_api_action_returns_exactly_what_the_catalog_declares(config, base_url, action, build_args):
    spec = spec_for(f"api.{action}")
    assert spec is not None, f"动作目录里没有 api.{action}"

    result = _run(config, action, build_args(base_url))

    assert result.ok, result.error
    assert set(result.data) == set(spec.returns) - {"error"}, (
        f"api.{action} 的返回字段与目录不一致："
        f"多出来 {set(result.data) - set(spec.returns)}，缺少 {set(spec.returns) - {'error'} - set(result.data)}"
    )


def test_failed_response_carries_error_field(config, base_url):
    """失败时多一个 error（目录里声明了它，但成功路径不会出现）。"""
    spec = spec_for("api.get")
    assert spec is not None
    assert "error" in spec.returns

    failed = _run(config, "get", {"url": f"{base_url}/boom"})
    assert failed.ok is False
    assert set(failed.data) == set(spec.returns)

    allowed = _run(config, "get", {"url": f"{base_url}/boom", "allow_error": True})
    assert allowed.ok is True
    assert "error" in allowed.data, "allow_error 放行后仍应保留 error，便于排查"


def test_failed_download_is_reported_as_failure(config, base_url):
    """下载失败不能判成功（以前无条件 success，4xx/5xx 也绿）。"""
    spec = spec_for("api.download")
    assert spec is not None

    result = _run(config, "download", {"url": f"{base_url}/boom"})

    assert result.error is not None
    assert set(result.data) == set(spec.returns), (
        "失败路径的字段应与目录一致（含 error）"
    )

    allowed = _run(config, "download", {"url": f"{base_url}/boom", "allow_error": True})
    assert allowed.ok is True
    assert allowed.error is None
    assert "error" in allowed.data, "allow_error 放行后仍应保留 error，便于排查"


def test_download_really_writes_the_file_with_get(config, base_url):
    """下载要看真效果：用 GET 发出去、文件真落盘、字节数对得上。

    桩服务只实现 do_GET：如果实现拿动作名当 HTTP 方法（`DOWNLOAD /path`），
    桩会回 501 + HTML 错误页 —— 这条测试当场就会红（修这个 bug 时正是这么发现的）。
    """
    from pathlib import Path

    result = _run(config, "download", {"url": f"{base_url}/bin"})

    assert result.ok, result.error
    assert result.data["method"] == "GET"
    target = Path(result.data["output"])
    assert target.exists(), f"下载没有落盘: {target}"
    assert target.read_bytes() == BINARY
    assert result.data["bytes"] == len(BINARY)
