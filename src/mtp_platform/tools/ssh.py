"""SSH 直连工具（paramiko）。

与基线 `ssh_adapter` 的关系：
- **策略逻辑整段复用**：`normalize_host` 与 `_assert_host_allowed`（白名单 / `allow_any_host`
  门槛）原样搬过来；
- **传输换了**：不再经 MCP 子进程，直接用 paramiko 建连、执行、SFTP 传输。
  因此不再需要解析 ssh-mcp 的格式化文本（`Command:/Exit code:/Stdout:`）——
  paramiko 直接给 `exit_code` / `stdout` / `stderr`。

安全：`host` 必须命中 `security.ssh_allow_hosts`；白名单为空 = 全部拒绝；
用例里的 `allow_any_host: true` 只有在配置层 `security.ssh_allow_any_host: true` 时才生效。
"""

from __future__ import annotations

import fnmatch
import time
from pathlib import Path
from typing import Any

from mtp_contracts.adapters import ActionResult, StepContext
from mtp_contracts.errors import ConfigError, PolicyDeniedError, ToolExecutionError
from .base import BaseTool

try:  # 未安装 paramiko 时给出明确提示，而不是 ImportError 炸栈
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None  # type: ignore[assignment]

_ACTIONS: dict[str, str] = {
    "execute": "在远端执行命令",
    "upload": "上传本地文件/目录到远端（SFTP）",
    "download": "从远端下载文件到本地（SFTP）",
}


def normalize_host(host: str) -> str:
    """归一化 host 后再做白名单比对。

    - 去掉 `[::1]` 这种 IPv6 方括号写法；
    - 去掉 `1.2.3.4:22` 这种端口后缀（IPv6 有多个冒号，不当作端口处理）；
    - 统一小写，让域名匹配与大小写无关。
    """
    value = str(host).strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value.count(":") == 1:  # IPv6 会有多个冒号，不会误伤
        value = value.split(":", 1)[0]
    return value.lower()


class SshTool(BaseTool):
    name = "ssh"

    def actions(self) -> dict[str, str]:
        return dict(_ACTIONS)

    # -- 白名单（复用基线逻辑）---------------------------------------------
    def _assert_host_allowed(self, host: str | None, allow_any: bool) -> None:
        if not host:
            raise ConfigError(
                "ssh 步骤缺少 host",
                adapter=self.name,
                detail="每次调用都要显式给 host + user，以及 password 或 ssh_key_filepath",
            )

        if allow_any:
            # 单个用例不得自行绕过策略：必须由配置层统一开启
            if not self.config.security.get("ssh_allow_any_host"):
                raise PolicyDeniedError(
                    "用例请求 allow_any_host，但配置未开启",
                    adapter=self.name,
                    detail="确需豁免时，在 mtp_config.yaml 设置 security.ssh_allow_any_host: true",
                )
            return

        allowed = self.config.security.get("ssh_allow_hosts") or []
        if not allowed:
            raise PolicyDeniedError(
                f"SSH 目标被拒绝（白名单为空）: {host}",
                adapter=self.name,
                detail="在 mtp_config.yaml 的 security.ssh_allow_hosts 中放行目标",
            )

        # IPv4 / IPv6 / 域名 / 通配符走同一套匹配，缺一不可。
        candidate = normalize_host(host)
        for pattern in allowed:
            if fnmatch.fnmatchcase(candidate, normalize_host(str(pattern))):
                return

        raise PolicyDeniedError(
            f"SSH 目标不在白名单: {host}",
            adapter=self.name,
            detail=f"允许: {', '.join(str(a) for a in allowed)}",
        )

    def pre_execute(self, action: str, args: dict[str, Any], context: StepContext) -> None:
        args = args or {}
        self._assert_host_allowed(args.get("host"), bool(args.get("allow_any_host")))

    # -- 连接 ---------------------------------------------------------------
    def _connect(self, args: dict[str, Any]):
        if paramiko is None:  # pragma: no cover - 依赖缺失路径
            raise ConfigError(
                "未安装 paramiko，无法使用 ssh 工具",
                adapter=self.name,
                detail="安装： uv sync --extra ssh",
            )
        if not args.get("user"):
            raise ConfigError("ssh 步骤缺少 user", adapter=self.name)

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.config.security.get("ssh_strict_host_key"):
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # 内网测试设备通常没有登记 host key；默认放行未知主机。
            # 安全要求高的环境请设 security.ssh_strict_host_key: true。
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_timeout = float(args.get("connect_timeout", 15))
        client.connect(
            hostname=str(args["host"]),
            port=int(args.get("port", 22)),
            username=str(args.get("user")),
            password=args.get("password"),
            key_filename=args.get("ssh_key_filepath"),
            timeout=connect_timeout,
            banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
        )
        return client

    # -- 执行 ---------------------------------------------------------------
    def do_execute(
        self, action: str, args: dict[str, Any], context: StepContext
    ) -> ActionResult:
        args = args or {}
        started = time.monotonic()
        if action == "execute":
            return self._run_command(args, started)
        if action == "upload":
            return self._transfer(args, started, upload=True)
        return self._transfer(args, started, upload=False)

    def _run_command(self, args: dict[str, Any], started: float) -> ActionResult:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ConfigError("ssh execute 缺少 command", adapter=self.name)

        timeout = float(args.get("timeout", 30))
        client = self._connect(args)
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
        finally:
            client.close()

        elapsed_ms = int((time.monotonic() - started) * 1000)
        data: dict[str, Any] = {
            "command": command,
            "exit_code": exit_code,
            "stdout": out,
            "stderr": err,
            "succeeded": exit_code == 0,
            "duration_ms": elapsed_ms,
            "host": args.get("host"),
        }

        ok_codes = args.get("ok_exit_codes", [0])
        if exit_code not in ok_codes:
            # 命令非零退出 = 步骤失败，但 exit_code/stdout/stderr 仍要留在 data 里供断言用
            return ActionResult(
                ok=False,
                action="execute",
                adapter=self.name,
                data=data,
                summary=f"{args.get('host')} 执行 `{command[:80]}` -> exit {exit_code}（{elapsed_ms}ms）",
                error=ToolExecutionError(
                    f"远程命令退出码 {exit_code}（期望 {ok_codes}）",
                    adapter=self.name,
                    action="execute",
                    detail=err or out,
                ),
            )

        return ActionResult.success(
            "execute",
            adapter=self.name,
            data=data,
            summary=f"{args.get('host')} 执行 `{command[:80]}` -> exit {exit_code}（{elapsed_ms}ms）",
        )

    def _transfer(self, args: dict[str, Any], started: float, *, upload: bool) -> ActionResult:
        host = args.get("host")
        client = self._connect(args)
        try:
            sftp = client.open_sftp()
            try:
                if upload:
                    local = Path(str(args.get("local_path") or args.get("local_dir") or ""))
                    remote = str(args.get("remote_path") or "")
                    if not local or not remote:
                        raise ConfigError("ssh upload 需要 local_path 与 remote_path", adapter=self.name)
                    if local.is_dir():
                        self._put_dir(sftp, local, remote)
                        summary = f"{host} 上传目录 {local} -> {remote}"
                    else:
                        sftp.put(str(local), remote)
                        summary = f"{host} 上传 {local} -> {remote}"
                    data = {"local_path": str(local), "remote_path": remote, "host": host}
                else:
                    remote = str(args.get("remote_file") or args.get("remote_path") or "")
                    local = Path(str(args.get("local_path") or ""))
                    if not remote or not str(local):
                        raise ConfigError("ssh download 需要 remote_file 与 local_path", adapter=self.name)
                    local.parent.mkdir(parents=True, exist_ok=True)
                    sftp.get(remote, str(local))
                    summary = f"{host} 下载 {remote} -> {local}"
                    data = {"remote_file": remote, "local_path": str(local), "host": host}
            finally:
                sftp.close()
        finally:
            client.close()

        elapsed_ms = int((time.monotonic() - started) * 1000)
        data["duration_ms"] = elapsed_ms
        return ActionResult.success(
            "upload" if upload else "download",
            adapter=self.name,
            data=data,
            summary=f"{summary}（{elapsed_ms}ms）",
        )

    @staticmethod
    def _put_dir(sftp: Any, local: Path, remote: str) -> None:
        """递归上传目录（远端不存在则创建）。"""
        try:
            sftp.mkdir(remote)
        except OSError:
            pass  # 已存在
        for child in sorted(local.iterdir()):
            target = f"{remote.rstrip('/')}/{child.name}"
            if child.is_dir():
                SshTool._put_dir(sftp, child, target)
            elif child.is_file():
                sftp.put(str(child), target)

__all__ = ["SshTool", "normalize_host"]
