"""HTTP 直连工具测试（自基线 api_runner 测试迁移，全部 mock，不发真实请求）。

覆盖：URL 构造、目标白名单与重定向复检（防 SSRF）、响应体上限、
上传/下载路径沙箱、代理绕行。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from mtp_platform.contracts.adapters import StepContext
from mtp_platform.contracts.errors import (
    AuthenticationError,
    ConfigError,
    McpUnavailableError,
    NetworkError,
    PolicyDeniedError,
    TimeoutError_,
    ToolExecutionError,
    classify_exception,
)
from mtp_platform.config import load_config
from mtp_platform.contracts.redaction import SecretRegistry, redact


@pytest.fixture(scope="module")
def config():
    return load_config()


def ctx(**kw) -> StepContext:
    return StepContext(**kw)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def test_api_requires_url_or_path(config):
    from mtp_platform.tools.http import ApiTool

    adapter = ApiTool(config)
    result = adapter.execute("get", {}, ctx())
    assert not result.ok
    assert result.error.code == "config_error"


def test_api_builds_url_from_env_base_url(config):
    from mtp_platform.tools.http import ApiTool

    adapter = ApiTool(config)
    assert (
        adapter._resolve_url({"path": "/api/me"}, ctx(env={"base_url": "http://127.0.0.1:8099"}))
        == "http://127.0.0.1:8099/api/me"
    )


def test_api_bypasses_proxy_for_loopback(config):
    import os

    from mtp_platform.tools.http import ApiTool

    adapter = ApiTool(config)
    os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:1")
    proxies = adapter._proxies_for("http://127.0.0.1:8099/api/me", {})
    assert proxies == {"http": None, "https": None}
    # 显式要求走代理时不绕过
    assert adapter._proxies_for("http://127.0.0.1:8099/x", {"use_proxy": True}) is None




# ---------------------------------------------------------------------------
# API：上传 / 下载 路径沙箱（回归 REVIEW-2026-09-12 的 P1-3）
# ---------------------------------------------------------------------------
def _api_adapter(config):
    from mtp_platform.tools.http import ApiTool

    return ApiTool(config)


def test_api_upload_rejects_paths_outside_allowed_roots(config):
    adapter = _api_adapter(config)
    for path in ("/etc/passwd", "artifacts/../../../etc/passwd", "~/.ssh/id_rsa"):
        result = adapter.execute(
            "post",
            {"url": "http://127.0.0.1:1/x", "files": [{"field": "f", "path": path}]},
            ctx(),
        )
        assert not result.ok, path
        assert result.error.code == "policy_denied", path


def test_api_upload_allows_file_inside_allowed_root(config):
    adapter = _api_adapter(config)
    root = config.api_upload_roots()[0]
    root.mkdir(parents=True, exist_ok=True)
    sample = root / "_api_sandbox_ok.txt"
    sample.write_text("hello", encoding="utf-8")
    try:
        prepared = adapter._prepare_files([{"field": "f", "path": str(sample)}], ctx())
        assert prepared and prepared[0][0] == "f"
    finally:
        sample.unlink(missing_ok=True)


def test_api_upload_rejects_symlink_escaping_allowed_root(config):
    """只做字符串前缀判断会被软链绕过，必须 resolve() 之后再判包含。"""
    adapter = _api_adapter(config)
    root = config.api_upload_roots()[0]
    root.mkdir(parents=True, exist_ok=True)
    link = root / "_api_sandbox_link"
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink("/etc/hosts", link)
    try:
        result = adapter.execute(
            "post",
            {"url": "http://127.0.0.1:1/x", "files": [{"field": "f", "path": str(link)}]},
            ctx(),
        )
        assert not result.ok
        assert result.error.code == "policy_denied"
    finally:
        if link.is_symlink():
            link.unlink()


def test_api_upload_can_be_disabled_entirely():
    from mtp_platform.tools.http import ApiTool
    from mtp_platform.config import PlatformConfig

    cfg = PlatformConfig(
        raw={"security": {"api_upload_roots": []}}, path=Path("/tmp/api_probe.yaml")
    )
    adapter = ApiTool(cfg)
    result = adapter.execute(
        "post",
        {"url": "http://127.0.0.1:1/x", "files": [{"field": "f", "path": "/etc/passwd"}]},
        ctx(),
    )
    assert not result.ok
    assert result.error.code == "policy_denied"


def test_api_download_output_must_stay_inside_download_root(config):
    from mtp_platform.tools.http import _resolve_within

    root = config.api_download_root()
    for bad in ("/tmp/evil.bin", "artifacts/../../evil.bin"):
        with pytest.raises(PolicyDeniedError):
            _resolve_within(Path(bad), [root], reason="下载输出")

    inside = root / "sub" / "ok.bin"
    assert _resolve_within(inside, [root], reason="下载输出") == inside.resolve()


def test_api_download_root_follows_artifact_override(monkeypatch, tmp_path):
    """默认下载目录必须跟随 MTP_ARTIFACT_ROOT，避免容器里写回源码目录。"""
    from mtp_platform.config import load_config

    target = tmp_path / "external-artifacts"
    monkeypatch.setenv("MTP_ARTIFACT_ROOT", str(target))
    config = load_config()

    assert config.api_download_root() == target


def test_resolve_within_returns_none_style_error_for_broken_roots():
    """允许目录列表为空时，任何路径都必须被拒。"""
    from mtp_platform.tools.http import _resolve_within

    with pytest.raises(PolicyDeniedError):
        _resolve_within(Path("/etc/hosts"), [], reason="上传文件")




# ---------------------------------------------------------------------------
# API：目标白名单 / 重定向复检 / 响应体上限（REVIEW-FOLLOWUP A 批）
# ---------------------------------------------------------------------------
def _api_config(**security):
    from mtp_platform.config import PlatformConfig

    return PlatformConfig(raw={"security": security}, path=Path("/tmp/api_ssrf_probe.yaml"))


class _FakeResponse:
    """够用的 requests.Response 替身：状态码 + 头 + 可分块读的 body。"""

    def __init__(self, status: int, headers=None, body: bytes = b""):
        self.status_code = status
        self.headers = dict(headers or {})
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=1):
        size = chunk_size or 1
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


def _patch_requests(monkeypatch, handler):
    """把 api_runner 里的 requests.request 换成假的，并返回调用记录。"""
    from mtp_platform.tools import http as api_runner

    calls: list[tuple[str, str]] = []

    def fake(method, url, **kwargs):
        calls.append((method, url))
        return handler(method, url, calls)

    monkeypatch.setattr(api_runner.requests, "request", fake)
    return calls


def test_api_rejects_non_http_schemes():
    from mtp_platform.tools.http import ApiTool

    adapter = ApiTool(_api_config())
    for url in ("file:///etc/passwd", "gopher://127.0.0.1:6379/_INFO", "ftp://x/y"):
        with pytest.raises(PolicyDeniedError):
            adapter._assert_target_allowed(url, {})


def test_api_blocks_cloud_metadata_by_default():
    """没配白名单也要挡住元数据地址 —— 它不可能是「被测服务」。"""
    from mtp_platform.tools.http import ApiTool

    adapter = ApiTool(_api_config())
    with pytest.raises(PolicyDeniedError) as excinfo:
        adapter._assert_target_allowed("http://169.254.169.254/latest/meta-data/", {})
    assert "169.254" in str(excinfo.value)


def test_api_allow_hosts_is_enforced_when_configured():
    from mtp_platform.tools.http import ApiTool

    adapter = ApiTool(_api_config(api_allow_hosts=["127.0.0.1", "*.internal.example"]))
    adapter._assert_target_allowed("http://127.0.0.1:8099/api/me", {})
    adapter._assert_target_allowed("https://api.internal.example/v1", {})
    with pytest.raises(PolicyDeniedError):
        adapter._assert_target_allowed("http://evil.example.com/", {})


def test_api_allow_hosts_empty_list_denies_everything():
    from mtp_platform.tools.http import ApiTool

    adapter = ApiTool(_api_config(api_allow_hosts=[]))
    with pytest.raises(PolicyDeniedError):
        adapter._assert_target_allowed("http://127.0.0.1/", {})


def test_api_allow_any_host_needs_config_opt_in():
    from mtp_platform.tools.http import ApiTool

    with pytest.raises(PolicyDeniedError):
        ApiTool(_api_config())._assert_target_allowed(
            "http://any.example/", {"allow_any_host": True}
        )
    ApiTool(_api_config(api_allow_any_host=True))._assert_target_allowed(
        "http://any.example/", {"allow_any_host": True}
    )


def test_api_rechecks_target_on_every_redirect(monkeypatch):
    """302 到元数据地址必须被拦：白名单内的起点不能给终点背书。"""
    from mtp_platform.tools.http import ApiTool

    def handler(method, url, calls):
        if url.endswith("/a"):
            return _FakeResponse(302, {"Location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError("不该真的跳到元数据地址")

    _patch_requests(monkeypatch, handler)
    adapter = ApiTool(_api_config(api_allow_hosts=["127.0.0.1"]))
    result = adapter.execute("get", {"url": "http://127.0.0.1:8099/a"}, ctx())
    assert not result.ok
    assert result.error.code == "policy_denied"


def test_api_follows_redirect_to_allowed_host(monkeypatch):
    from mtp_platform.tools.http import ApiTool

    def handler(method, url, calls):
        if url.endswith("/a"):
            return _FakeResponse(302, {"Location": "/b"})
        return _FakeResponse(200, {"Content-Type": "application/json"}, b'{"ok": true}')

    calls = _patch_requests(monkeypatch, handler)
    adapter = ApiTool(_api_config(api_allow_hosts=["127.0.0.1"]))
    result = adapter.execute("get", {"url": "http://127.0.0.1:8099/a"}, ctx())
    assert result.ok, result.error
    assert [u for _, u in calls] == ["http://127.0.0.1:8099/a", "http://127.0.0.1:8099/b"]
    assert result.data["json"] == {"ok": True}


def test_api_redirect_downgrades_post_to_get(monkeypatch):
    """重定向后不带 body：不把原始 POST 数据发到一个没校验过语义的地址。"""
    from mtp_platform.tools.http import ApiTool

    def handler(method, url, calls):
        if url.endswith("/a"):
            return _FakeResponse(307, {"Location": "/b"})
        return _FakeResponse(200, {}, b"ok")

    calls = _patch_requests(monkeypatch, handler)
    adapter = ApiTool(_api_config(api_allow_hosts=["127.0.0.1"]))
    result = adapter.execute(
        "post", {"url": "http://127.0.0.1:8099/a", "json": {"pwd": "x"}}, ctx()
    )
    assert result.ok, result.error
    assert calls[1][0] == "GET"


def test_api_redirect_hops_are_capped(monkeypatch):
    from mtp_platform.tools.http import ApiTool

    def handler(method, url, calls):
        return _FakeResponse(302, {"Location": f"/next{len(calls)}"})

    _patch_requests(monkeypatch, handler)
    adapter = ApiTool(_api_config(api_allow_hosts=["127.0.0.1"], api_max_redirects=2))
    result = adapter.execute("get", {"url": "http://127.0.0.1:8099/a"}, ctx())
    assert result.error.code == "policy_denied"
    assert "重定向" in str(result.error)


def test_api_response_over_limit_is_rejected(monkeypatch):
    from mtp_platform.tools.http import ApiTool

    big = b"x" * 100
    _patch_requests(
        monkeypatch,
        lambda m, u, c: _FakeResponse(200, {"Content-Length": str(len(big))}, big),
    )
    adapter = ApiTool(_api_config(api_max_response_bytes=10))
    result = adapter.execute("get", {"url": "http://127.0.0.1:8099/a"}, ctx())
    assert result.error.code == "policy_denied"
    assert "响应体" in str(result.error)


def test_api_response_over_limit_without_content_length(monkeypatch):
    """分块传输没有 Content-Length，也得靠边读边计数拦下来。"""
    from mtp_platform.tools.http import ApiTool

    _patch_requests(monkeypatch, lambda m, u, c: _FakeResponse(200, {}, b"y" * 5000))
    adapter = ApiTool(_api_config(api_max_response_bytes=64))
    result = adapter.execute("get", {"url": "http://127.0.0.1:8099/a"}, ctx())
    assert result.error.code == "policy_denied"


def test_api_response_within_limit_is_returned(monkeypatch):
    from mtp_platform.tools.http import ApiTool

    _patch_requests(
        monkeypatch,
        lambda m, u, c: _FakeResponse(
            200, {"Content-Type": "application/json; charset=utf-8"}, b'{"n": 1}'
        ),
    )
    adapter = ApiTool(_api_config(api_max_response_bytes=1024))
    result = adapter.execute("get", {"url": "http://127.0.0.1:8099/a"}, ctx())
    assert result.ok, result.error
    assert result.data["json"] == {"n": 1}
    assert result.data["text"] == '{"n": 1}'

