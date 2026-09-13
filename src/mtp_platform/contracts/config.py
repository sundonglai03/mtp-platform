"""配置数据模型。

只含**数据模型**（`McpServerConfig` / `PlatformConfig`）与读取访问器；
**文件加载与合并**（`load_config` / 环境变量展开 / 默认路径选择）在 `mtp-platform`。

所有相对路径按 `PlatformConfig.base_dir` 解析（由加载器注入），
因此本模块不依赖任何"项目根"全局变量 —— 这是它能被四个项目共享的前提。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .redaction import DEFAULT_PLACEHOLDER, DEFAULT_REDACT_KEYS

# ${VAR} 与 ${VAR:-默认值}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# 目前客户端只实现了 stdio 子进程启动。与其让配置看起来支持 HTTP、实际却按 stdio
# 去拉进程、报一个误导性的错误，不如在这里直接拒绝。
SUPPORTED_TRANSPORTS = ("stdio",)


@dataclass
class McpServerConfig:
    """一个 MCP server 的启动参数。

    `transport` 目前只支持 `"stdio"`：非 stdio 会在 `PlatformConfig.mcp_server()`
    里被直接拒绝，见 `SUPPORTED_TRANSPORTS`。
    """

    name: str
    transport: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_sec: float = 30.0
    cwd: str = ""

    def resolved_env(self) -> dict[str, str]:
        """完整环境变量 = 当前进程环境 + 配置里的覆盖项。

        必须带上 PATH 等基础变量，否则 `node` 这类命令会找不到。
        """
        merged = dict(os.environ)
        merged.update({str(k): str(v) for k, v in self.env.items()})
        return merged


@dataclass
class PlatformConfig:
    """平台配置的数据模型 + 访问器。

    `base_dir` 是解析相对路径的基准（由加载器注入为"项目根"）。
    """

    raw: dict[str, Any]
    path: Path
    # 引用了但当前环境没设置的环境变量（名字去重、排序后给 doctor 用）
    missing_env: list[str] = field(default_factory=list)
    # 相对路径解析基准；默认取当前工作目录，加载器会显式传入项目根
    base_dir: Path = field(default_factory=lambda: Path.cwd())

    # --- 分区访问 ---------------------------------------------------------
    @property
    def mcp(self) -> dict[str, Any]:
        return self.raw.get("mcp", {}) or {}

    @property
    def security(self) -> dict[str, Any]:
        return self.raw.get("security", {}) or {}

    @property
    def evidence(self) -> dict[str, Any]:
        return self.raw.get("evidence", {}) or {}

    @property
    def reporting(self) -> dict[str, Any]:
        return self.raw.get("reporting", {}) or {}

    @property
    def runner(self) -> dict[str, Any]:
        return self.raw.get("runner", {}) or {}

    # --- 派生 -------------------------------------------------------------
    def mcp_server(self, name: str) -> McpServerConfig:
        servers = self.mcp
        if name not in servers:
            raise ConfigError(
                f"MCP server 未配置: {name}",
                detail=f"可用: {sorted(servers)}（配置文件 {self.path}）",
            )
        entry = servers[name] or {}

        # 不要给「未实现的能力」留后门：transport 写 http 会静默按 stdio 处理，
        # 最后报出一个和真实原因无关的启动错误。
        transport = str(entry.get("transport", "stdio"))
        if transport not in SUPPORTED_TRANSPORTS:
            raise ConfigError(
                f"MCP server '{name}' 的 transport 不受支持: {transport}",
                detail=(
                    f"当前只实现了 {'/'.join(SUPPORTED_TRANSPORTS)}；"
                    "要接 HTTP/Streamable HTTP 需先在 mcp_client 里实现对应客户端"
                ),
            )

        return McpServerConfig(
            name=name,
            transport=transport,
            command=str(entry.get("command", "")),
            args=[str(a) for a in (entry.get("args") or [])],
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            timeout_sec=float(entry.get("timeout_sec", 30)),
            # 子进程的 cwd 默认落在证据目录里的一个专用暂存区。
            # 有些 MCP（@playwright/mcp 的 screenshot/snapshot 传 filename）会把文件
            # 按**相对路径**写到自己的 cwd —— 不设的话就会散落到项目根目录。
            cwd=str(entry.get("cwd") or self.tool_cwd()),
        )

    def mcp_names(self) -> list[str]:
        return sorted(self.mcp)

    def redact_keys(self) -> list[str]:
        keys = self.security.get("redact_keys")
        return [str(k) for k in keys] if keys else list(DEFAULT_REDACT_KEYS)

    def redact_placeholder(self) -> str:
        return str(self.security.get("redact_placeholder") or DEFAULT_PLACEHOLDER)

    def artifact_root(self) -> Path:
        """证据/产物根目录。

        优先级：`MTP_ARTIFACT_ROOT` 环境变量 > 配置 `evidence.root` > 默认 `artifacts`。
        设置环境变量可把全部产物（报告、历史、ledger、MCP cwd、审计）一次性移出项目目录，
        便于在 CI / 容器里把产物挂到独立卷，而不是污染源码树。
        """
        env = os.environ.get("MTP_ARTIFACT_ROOT")
        if env:
            root = Path(env).expanduser()
            return root if root.is_absolute() else (self.base_dir / root)
        root = Path(str(self.evidence.get("root", "artifacts")))
        return root if root.is_absolute() else (self.base_dir / root)

    def report_dir(self) -> Path:
        configured = self.reporting.get("output_dir")
        if configured is None:
            # 缺省跟随产物根目录，确保 MTP_ARTIFACT_ROOT 覆盖时报告也落过去
            return self.artifact_root() / "reports"
        out = Path(str(configured))
        return out if out.is_absolute() else (self.base_dir / out)

    def history_file(self) -> Path:
        configured = self.reporting.get("history_file")
        if configured is None:
            # 缺省跟随产物根目录
            return self.artifact_root() / "history.jsonl"
        out = Path(str(configured))
        return out if out.is_absolute() else (self.base_dir / out)

    def tool_cwd(self) -> Path:
        """MCP 子进程的工作目录：收拢工具按相对路径写出的文件。

        之前 `playwright.screenshot` 传相对 `filename` 时，截图会直接落在项目根目录，
        把工作区搞脏（CI 里也会污染 workspace）。统一丢进证据目录下的 `_toolcwd/`。
        """
        directory = self.artifact_root() / "_toolcwd"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def api_upload_roots(self) -> list[Path]:
        """api 步骤允许读取（上传）的本地目录。

        相对路径按项目根解析。缺省给一组安全目录；显式配成 `[]` 表示
        **禁止一切上传**（安全优先的团队可以这么设）。
        """
        raw = self.security.get("api_upload_roots")
        if raw is None:
            raw = ["fixtures", "artifacts", "case-schema"]
        roots: list[Path] = []
        for item in raw or []:
            candidate = Path(str(item)).expanduser()
            roots.append(candidate if candidate.is_absolute() else (self.base_dir / candidate))
        return roots

    def api_download_root(self) -> Path:
        """api `download` action 的落盘根目录（默认落在证据目录下）。"""
        raw = self.security.get("api_download_root")
        if raw is None:
            return self.artifact_root()
        candidate = Path(str(raw)).expanduser()
        return candidate if candidate.is_absolute() else (self.base_dir / candidate)

    def api_allow_hosts(self) -> list[str] | None:
        """api 步骤允许访问的目标主机（fnmatch 通配）。

        返回 `None` = **未配置**，此时只挡「谁都不会主动去测」的地址
        （云元数据 / 链路本地），内网被测服务照常可连；
        返回列表 = 白名单模式，不在列表内一律拒绝；空列表 = 全部拒绝。

        之所以区分「未配置」和「空列表」：内网设备测试是本平台的主场景，
        默认全拒会把正常用法一起挡掉；但一旦显式配置，就必须严格按配置执行。
        """
        raw = self.security.get("api_allow_hosts")
        if raw is None:
            return None
        return [str(h) for h in (raw or [])]

    def api_allow_any_host(self) -> bool:
        """是否允许用例用 `allow_any_host: true` 自行豁免 api 目标白名单。"""
        return bool(self.security.get("api_allow_any_host", False))

    def api_max_response_bytes(self) -> int:
        """单个 api 响应允许读取的最大字节数（默认 5 MiB）。"""
        raw = self.security.get("api_max_response_bytes", 5 * 1024 * 1024)
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"security.api_max_response_bytes 不是整数: {raw!r}", detail=str(exc)
            ) from exc
        if value <= 0:
            raise ConfigError(
                f"security.api_max_response_bytes 必须为正整数: {value}",
                detail="设为 0 等于拒绝所有响应，通常不是本意",
            )
        return value

    def api_max_redirects(self) -> int:
        """api 请求最多跟随多少次重定向（默认 5）。"""
        raw = self.security.get("api_max_redirects", 5)
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"security.api_max_redirects 不是整数: {raw!r}", detail=str(exc)) from exc
        return max(0, value)

    # --- 体检 -------------------------------------------------------------
    def path_problems(self) -> list[str]:
        """命令 / 脚本路径层面的可疑点（供 `doctor` 与 CI 使用）。

        刻意**不**在加载阶段做这些检查：配置加载失败会挡住 `validate` 这类根本
        不需要 MCP 的命令，而路径不对只是「跑不了测试」，应当在 doctor 里被点名。
        """
        problems: list[str] = []
        for name in self.mcp_names():
            entry = self.mcp.get(name) or {}
            command = str(entry.get("command") or "").strip()
            if not command:
                problems.append(
                    f"mcp.{name}.command 为空"
                    + self._missing_hint(entry.get("command"))
                    + "（用 ${VAR} 引用环境变量，或在 mtp_config.local.yaml 里覆盖）"
                )
                continue

            if "/" not in command and not command.startswith("~"):
                if shutil.which(command) is None:
                    problems.append(
                        f"mcp.{name}.command 在 PATH 中找不到: {command}"
                        + self._missing_hint(entry.get("command"))
                    )
            else:
                target = Path(os.path.expanduser(command))
                if not target.exists():
                    problems.append(
                        f"mcp.{name}.command 不存在: {command}"
                        + self._missing_hint(entry.get("command"))
                    )
                elif not os.access(target, os.X_OK):
                    problems.append(f"mcp.{name}.command 没有可执行权限: {command}")

            # 参数里的脚本路径（node xxx/cli.js 这种）同样值得提前查一遍
            for arg in entry.get("args") or []:
                text = str(arg)
                if not (text.endswith((".js", ".py", ".sh")) or text.startswith(("~", "/", "./"))):
                    continue
                script = Path(os.path.expanduser(text))
                if script.is_absolute() and not script.exists():
                    problems.append(
                        f"mcp.{name}.args 里的脚本不存在: {text}"
                        + self._missing_hint(arg)
                    )

        if self.missing_env:
            problems.append(
                "以下环境变量未设置，相关配置被当成空值: " + ", ".join(self.missing_env)
            )
        return problems

    def _missing_hint(self, raw: Any) -> str:
        """如果这段配置引用了未设置的环境变量，把变量名说出来。"""
        if not isinstance(raw, str):
            return ""
        used = [m.group(1) for m in _ENV_REF.finditer(raw)]
        missing = [v for v in used if v in self.missing_env]
        return f"（环境变量未设置: {', '.join(missing)}）" if missing else ""

    def runner_default(self, key: str, fallback: Any) -> Any:
        return self.runner.get(key, fallback)
