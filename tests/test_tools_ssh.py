"""SSH 直连工具测试（不连真实主机）。

- 白名单 / allow_any_host 策略：复用基线 ssh_adapter 的验收点；
- execute / upload / download：用假的 paramiko 模块驱动，不建真连接。
"""

from __future__ import annotations

import tempfile
import types
from pathlib import Path

import pytest

from mtp_contracts.adapters import StepContext
from mtp_contracts.config import PlatformConfig
from mtp_platform.tools import ssh as ssh_module
from mtp_platform.tools.ssh import SshTool, normalize_host


def _cfg(**security) -> PlatformConfig:
    return PlatformConfig(raw={"security": security}, path=Path("mtp_config.yaml"))


@pytest.fixture
def fake_paramiko(monkeypatch):
    """假的 paramiko：可配置 stdout/stderr/exit_code，并记录调用。"""
    state = types.SimpleNamespace(stdout="", stderr="", exit_code=0, clients=[])

    class _Channel:
        def __init__(self, code):
            self._code = code

        def recv_exit_status(self):
            return self._code

    class _Stream:
        def __init__(self, text, code):
            self._text = text
            self.channel = _Channel(code)

        def read(self):
            return self._text.encode("utf-8")

    class _SFTP:
        def __init__(self):
            self.puts = []
            self.gets = []
            self.mkdirs = []

        def put(self, local, remote):
            self.puts.append((str(local), remote))

        def get(self, remote, local):
            self.gets.append((remote, str(local)))

        def mkdir(self, path):
            self.mkdirs.append(path)

        def close(self):
            pass

    class _Client:
        def __init__(self):
            self.commands = []
            self.closed = False
            self.sftp = _SFTP()
            self.opts = {}
            self.policy = None
            state.clients.append(self)

        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kw):
            self.opts = kw

        def exec_command(self, command, timeout=None):
            self.commands.append((command, timeout))
            return None, _Stream(state.stdout, state.exit_code), _Stream(state.stderr, 0)

        def open_sftp(self):
            return self.sftp

        def close(self):
            self.closed = True

    module = types.SimpleNamespace(
        SSHClient=_Client,
        AutoAddPolicy=lambda: "auto",
        RejectPolicy=lambda: "reject",
    )
    monkeypatch.setattr(ssh_module, "paramiko", module)
    state.client_cls = _Client
    return state


def _ctx(**kw) -> StepContext:
    return StepContext(**kw)


# --- 归一化 / 白名单（策略逻辑，复用基线验收点）----------------------------
def test_normalize_host_strips_brackets_and_port():
    assert normalize_host("[::1]") == "::1"
    assert normalize_host("192.168.1.10:22") == "192.168.1.10"
    assert normalize_host("Host.Example.COM") == "host.example.com"
    assert normalize_host("fe80::1") == "fe80::1"


def test_missing_host_is_config_error(config):
    result = SshTool(config).execute("execute", {"command": "uptime"}, _ctx())
    assert result.ok is False
    assert result.error.code == "config_error"


def test_host_outside_whitelist_is_denied(config):
    result = SshTool(config).execute(
        "execute", {"host": "8.8.8.8", "user": "u", "command": "uptime"}, _ctx()
    )
    assert result.ok is False
    assert result.error.code == "policy_denied"


def test_empty_whitelist_denies_everything():
    tool = SshTool(_cfg(ssh_allow_hosts=[]))
    result = tool.execute("execute", {"host": "127.0.0.1", "user": "u", "command": "x"}, _ctx())
    assert result.ok is False
    assert result.error.code == "policy_denied"


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.5.7", "10.0.0.9", "172.16.3.4"])
def test_whitelisted_hosts_pass(host, config, fake_paramiko):
    fake_paramiko.stdout = "ok\n"
    result = SshTool(config).execute(
        "execute", {"host": host, "user": "u", "command": "echo ok"}, _ctx()
    )
    assert result.ok, result.error


def test_allow_any_host_requires_config_opt_in(config):
    tool = SshTool(config)  # 仓库配置里 ssh_allow_any_host: false
    result = tool.execute(
        "execute",
        {"host": "8.8.8.8", "user": "u", "command": "x", "allow_any_host": True},
        _ctx(),
    )
    assert result.ok is False
    assert result.error.code == "policy_denied"


def test_allow_any_host_works_when_config_enables_it(fake_paramiko):
    fake_paramiko.stdout = "ok\n"
    tool = SshTool(_cfg(ssh_allow_hosts=[], ssh_allow_any_host=True))
    result = tool.execute(
        "execute",
        {"host": "8.8.8.8", "user": "u", "command": "x", "allow_any_host": True},
        _ctx(),
    )
    assert result.ok, result.error


# --- execute ---------------------------------------------------------------
def test_execute_returns_structured_fields(config, fake_paramiko):
    fake_paramiko.stdout = "hello\n"
    fake_paramiko.stderr = ""
    fake_paramiko.exit_code = 0

    result = SshTool(config).execute(
        "execute", {"host": "127.0.0.1", "user": "u", "command": "echo hello"}, _ctx()
    )
    assert result.ok
    assert result.data["exit_code"] == 0
    assert result.data["stdout"].strip() == "hello"
    assert result.data["succeeded"] is True
    assert "echo hello" in result.summary


def test_nonzero_exit_fails_step_but_keeps_exit_code(config, fake_paramiko):
    fake_paramiko.stdout = ""
    fake_paramiko.stderr = "boom\n"
    fake_paramiko.exit_code = 3

    result = SshTool(config).execute(
        "execute", {"host": "127.0.0.1", "user": "u", "command": "false"}, _ctx()
    )
    assert result.ok is False
    assert result.error.code == "tool_failed"
    # 关键：失败也要把 exit_code / stderr 留在 data 里供断言用
    assert result.data["exit_code"] == 3
    assert result.data["stderr"].strip() == "boom"


def test_execute_missing_command(config, fake_paramiko):
    result = SshTool(config).execute("execute", {"host": "127.0.0.1", "user": "u"}, _ctx())
    assert result.ok is False
    assert result.error.code == "config_error"


def test_unknown_action_is_rejected(config):
    result = SshTool(config).execute("reboot", {"host": "127.0.0.1"}, _ctx())
    assert result.ok is False
    assert result.error.code == "config_error"


# --- upload / download -----------------------------------------------------
def test_upload_file(config, fake_paramiko):
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "a.txt"
        local.write_text("hi", encoding="utf-8")

        result = SshTool(config).execute(
            "upload",
            {
                "host": "127.0.0.1",
                "user": "u",
                "local_path": str(local),
                "remote_path": "/tmp/a.txt",
            },
            _ctx(),
        )

    assert result.ok, result.error
    client = fake_paramiko.clients[-1]
    assert client.sftp.puts == [(str(local), "/tmp/a.txt")]
    assert client.closed is True  # 传完要断开


def test_upload_directory_creates_remote_and_recurses(config, fake_paramiko):
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "dir"
        (src / "sub").mkdir(parents=True)
        (src / "f1.txt").write_text("1", encoding="utf-8")
        (src / "sub" / "f2.txt").write_text("2", encoding="utf-8")

        result = SshTool(config).execute(
            "upload",
            {"host": "127.0.0.1", "user": "u", "local_dir": str(src), "remote_path": "/tmp/dir"},
            _ctx(),
        )

    assert result.ok, result.error
    client = fake_paramiko.clients[-1]
    assert "/tmp/dir" in client.sftp.mkdirs  # 远端目录会先创建
    targets = sorted(remote for _, remote in client.sftp.puts)
    assert targets == ["/tmp/dir/f1.txt", "/tmp/dir/sub/f2.txt"]


def test_download_file(config, fake_paramiko):
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out" / "b.txt"
        result = SshTool(config).execute(
            "download",
            {
                "host": "127.0.0.1",
                "user": "u",
                "remote_file": "/var/log/x.log",
                "local_path": str(target),
            },
            _ctx(),
        )
        assert target.parent.is_dir()  # 下载前会先建父目录

    assert result.ok, result.error
    client = fake_paramiko.clients[-1]
    assert client.sftp.gets == [("/var/log/x.log", str(target))]


# --- 依赖缺失 --------------------------------------------------------------
def test_missing_paramiko_gives_clear_error(config, monkeypatch):
    monkeypatch.setattr(ssh_module, "paramiko", None)
    result = SshTool(config).execute(
        "execute", {"host": "127.0.0.1", "user": "u", "command": "x"}, _ctx()
    )
    assert result.ok is False
    assert result.error.code == "config_error"
    assert "paramiko" in result.error.message
