"""HTTP API 执行器。

以适配器形式接入统一接口：`execute(action, args, context)`，与 SSH/MySQL/
Playwright 完全同构，因此编排器不需要为「接口测试」写任何特例。

要点：
- **不经过浏览器**，直接用 `requests` 发请求；
- 支持 GET/POST/PUT/PATCH/DELETE（外加 `request` 透传方法、`download` 落文件）；
- 参数位置齐全：query / header / cookie / JSON / 表单 / 文件上传；
- 变量提取：`extract: {名称: "$.json.path"}`，结果放进 `steps.<id>.extracted`；
- **日志与证据里不出现密码、Token、Cookie 原文**——请求头在写入证据前会被脱敏，
  摘要里也绝不带 Authorization。

SSRF 防护（三条，缺一不可）：
- **目标校验**：协议只允许 http/https；`security.api_allow_hosts` 配了就按白名单
  放行，没配也至少挡掉云元数据（169.254.0.0/16）这类链路本地地址；
- **每跳重校验**：重定向是**自己**跟的（`allow_redirects=False`），否则
  「先请求白名单内的 A，再 302 到元数据地址」就能绕过白名单；
- **响应体上限**：`stream=True` 边读边计数，超过 `security.api_max_response_bytes`
  立刻断开，不让一个超大响应吃掉内存。
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json as _json
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .base import ActionResult, BaseTool, StepContext
from mtp_contracts.errors import (
    ConfigError,
    NetworkError,
    PolicyDeniedError,
    TimeoutError_,
    ToolExecutionError,
    classify_exception,
)

try:  # requests 缺失时给出明确提示，而不是 ImportError 炸栈
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

_ACTIONS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "download"}

# 这些请求头只用于发请求，绝不进摘要
_SENSITIVE_HEADERS = ("authorization", "cookie", "proxy-authorization", "x-api-key")

_READ_CHUNK = 65536

# 无论白名单怎么配，这些目标一律拒绝：它们不是「被测服务」，而是云厂商元数据 /
# 链路本地 / 保留地址。命中即视为 SSRF 利用。
# 注意：刻意**不**挡 loopback 与 RFC1918 —— 打本机和内网设备是本平台的正常用法。
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("fe80::/10"),
)


def _resolve_within(path: Path, roots: list[Path], *, reason: str) -> Path:
    """把 path 归一化后确认它落在 `roots` 之内，返回解析后的绝对路径。

    必须用 `resolve()` 展开 `..` 与**符号链接**后再做「祖先包含」判断：
    只比较字符串前缀会被 `../../` 和指向外部的软链接绕过。
    """
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ConfigError(
            f"{reason}路径无法解析: {candidate}", adapter="api", detail=str(exc)
        ) from exc

    resolved_roots: list[Path] = []
    for root in roots:
        try:
            resolved_roots.append(Path(root).expanduser().resolve())
        except OSError:  # 根目录本身不可解析就跳过
            continue

    for root in resolved_roots:
        if resolved == root or root in resolved.parents:
            return resolved

    raise PolicyDeniedError(
        f"{reason}路径越界: {candidate}",
        adapter="api",
        detail=(
            f"允许的目录: {', '.join(str(r) for r in resolved_roots) or '(无)'}"
            "；可在 mtp_config.yaml 的 security.api_upload_roots / api_download_root 调整"
        ),
    )


def _jsonpath_find(expr: str, data: Any) -> list[Any]:
    if data is None:
        return []
    if expr in ("$", ""):
        return [data]
    try:
        from jsonpath_ng.ext import parse

        return [m.value for m in parse(expr).find(data)]
    except Exception:  # noqa: BLE001
        return []


@dataclass
class ApiResponse:
    """`requests.Response` 的最小替身，只为**可控地**拿到带上限的响应体。

    不直接往外传 `requests.Response`：它的 `.content` 会把整个流一次性读进内存，
    而响应体上限要求「边读边计数、超限即停」。同时它也不依赖 requests 的内部
    字段（如 `_content`），换 HTTP 客户端时不用跟着改。
    """

    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes
    _text: str | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def text(self) -> str:
        if self._text is None:
            charset = "utf-8"
            for part in (self.headers.get("Content-Type") or "").split(";"):
                if part.strip().lower().startswith("charset="):
                    charset = part.split("=", 1)[1].strip().strip('"\'')
                    break
            self._text = self.content.decode(charset, errors="replace")
        return self._text

    def json(self) -> Any:
        return _json.loads(self.text)


class ApiTool(BaseTool):
    name = "api"

    def actions(self) -> dict[str, str]:
        return {action: f"HTTP {action.upper()}" for action in sorted(_ACTIONS)}

    def do_execute(self, action: str, args: dict[str, Any], context: StepContext) -> ActionResult:
        if requests is None:  # pragma: no cover
            raise ConfigError("缺少 requests 依赖", detail="pip install requests")

        args = args or {}
        url = self._resolve_url(args, context)
        # 动作名不一定等于 HTTP 方法：download 就是 GET（以前这里直接取 action，
        # 于是请求以 `DOWNLOAD /path HTTP/1.1` 发出去，真服务器只会回 405/501）。
        method = str(args.get("method") or ("GET" if action == "download" else action)).upper()
        timeout = float(args.get("timeout_sec") or 30)

        headers = {str(k): str(v) for k, v in (args.get("headers") or {}).items()}
        cookies = {str(k): str(v) for k, v in (args.get("cookies") or {}).items()}
        params = args.get("query") or args.get("params") or None
        json_body = args.get("json")
        form = args.get("form") or args.get("data")
        files = self._prepare_files(args.get("files"), context)

        started = time.monotonic()
        response = self._send(
            method=method,
            url=url,
            args=args,
            params=params,
            headers=headers or None,
            cookies=cookies or None,
            json_body=json_body,
            form=form,
            files=files,
            timeout=timeout,
        )
        duration_ms = int((time.monotonic() - started) * 1000)

        if action == "download":
            # 输出必须落在证据目录内；不给 output 时也落到那里，
            # 绝不用 cwd（否则下载产物会直接写进项目根目录）。
            root = self.config.api_download_root()
            raw_output = args.get("output")
            if raw_output:
                target = _resolve_within(Path(str(raw_output)), [root], reason="下载输出")
            else:
                target = (
                    root
                    / (context.run_id or "adhoc")
                    / f"download-{context.step_id or 'out'}.bin"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
            data = {
                # 与动作目录里 api.download 的返回契约逐字对齐：下载落的是文件，
                # 没有 json / text / extracted，但状态码与请求信息要给全，
                # 否则用例引用了"契约说有、实现没有"的字段会到运行时才炸。
                "http_status": response.status_code,
                "status_code": response.status_code,
                "ok": response.ok,
                "url": url,
                "method": method,
                "output": str(target),
                "bytes": len(response.content),
                "duration_ms": duration_ms,
            }
            if not response.ok:
                # 以前这里无条件 success：4xx/5xx 也判「步骤通过」，等于把下载失败吃掉。
                data["error"] = f"HTTP {response.status_code}"
            ok = response.ok or bool(args.get("allow_error"))
            return ActionResult(
                ok=ok,
                action=action,
                adapter=self.name,
                data=data,
                summary=f"{method} {url} -> {response.status_code}，已保存 {len(response.content)} 字节",
                error=None
                if ok
                else ToolExecutionError(
                    f"HTTP {response.status_code}",
                    adapter=self.name,
                    action=action,
                    detail=f"下载失败，落盘内容不是目标文件：{response.text[:300]}",
                ),
            )

        body_json: Any = None
        try:
            body_json = response.json()
        except ValueError:
            body_json = None

        extracted: dict[str, Any] = {}
        for name, expr in (args.get("extract") or {}).items():
            matches = _jsonpath_find(str(expr), body_json)
            extracted[str(name)] = matches[0] if matches else None

        data: dict[str, Any] = {
            "http_status": response.status_code,
            "status_code": response.status_code,
            "ok": response.ok,
            "duration_ms": duration_ms,
            "url": url,
            "method": method,
            # 响应头里可能带 Set-Cookie，交给证据层按字段名脱敏后再落盘
            "headers": dict(response.headers),
            "json": body_json,
            "text": response.text,
            "extracted": extracted,
        }
        if not response.ok:
            data["error"] = f"HTTP {response.status_code}"

        ok = response.ok or bool(args.get("allow_error"))
        return ActionResult(
            ok=ok,
            action=action,
            adapter=self.name,
            data=data,
            raw=[{"type": "text", "text": response.text[:4000]}],
            summary=self._summarize(method, url, response.status_code, duration_ms),
            error=None
            if ok
            else ToolExecutionError(
                f"HTTP {response.status_code}",
                adapter=self.name,
                action=action,
                detail=response.text[:500],
            ),
        )

    # -- 发送：重定向与响应体都要受控 ---------------------------------------
    def _send(
        self,
        method: str,
        url: str,
        *,
        args: dict[str, Any],
        params: Any,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        json_body: Any,
        form: Any,
        files: Any,
        timeout: float,
    ) -> ApiResponse:
        """发一次请求，并**自己**处理重定向与响应体上限。

        与 requests 默认行为的两处不同，正是安全的必要代价：

        1. `allow_redirects=False`：每跳都先重新校验目标再继续。交给 requests
           自动跟随的话，「请求白名单内的 A，被 302 带到 169.254.169.254」
           可以直接绕过白名单；
        2. `stream=True` + 边读边计数：超过 `api_max_response_bytes` 立刻断开。
        """
        max_bytes = self.config.api_max_response_bytes()
        max_redirects = self.config.api_max_redirects()
        follow = bool(args.get("allow_redirects", True))

        current = url
        hops = 0
        while True:
            # 每一跳都校验：起点合法不代表跳到的地方也合法
            self._assert_target_allowed(current, args)
            proxies = self._proxies_for(current, args)
            try:
                raw = requests.request(
                    method=method,
                    url=current,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    json=json_body if json_body is not None else None,
                    data=form if (json_body is None and files is None) else None,
                    files=files,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                    proxies=proxies,
                )
            except requests.exceptions.Timeout as exc:
                raise TimeoutError_(
                    f"请求超时（{timeout}s）: {method} {current}", adapter=self.name
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise classify_exception(exc, adapter=self.name) from exc

            with raw:
                status = int(raw.status_code)
                resp_headers = {str(k): str(v) for k, v in raw.headers.items()}
                content = self._read_capped(raw, max_bytes, url=current)

            location = resp_headers.get("Location") if 300 <= status < 400 else None
            if not (location and follow):
                return ApiResponse(
                    status_code=status, url=current, headers=resp_headers, content=content
                )

            hops += 1
            if hops > max_redirects:
                raise PolicyDeniedError(
                    f"重定向次数超过上限（{max_redirects}）: {url}",
                    adapter=self.name,
                    detail="确需更多跳数可在 security.api_max_redirects 调整；"
                    "反复重定向通常说明目标在把请求往别处带，值得先确认",
                )

            current = urljoin(current, str(location))
            # 重定向后一律降级为 GET 并丢掉 body：requests 对 301/302 也是这么做的，
            # 这里对 307/308 同样处理 —— 不把原始 POST 数据发到一个没校验过语义的地址。
            if method not in ("GET", "HEAD"):
                method = "GET"
                json_body = None
                form = None
                files = None

    def _read_capped(self, response: Any, max_bytes: int, *, url: str) -> bytes:
        """边读边计数；超限立刻停手，不把完整响应体读进内存。"""
        declared = response.headers.get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) > max_bytes:
            raise self._too_large(int(declared), max_bytes, url)

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=_READ_CHUNK):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise self._too_large(total, max_bytes, url)
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _too_large(actual: int, limit: int, url: str) -> PolicyDeniedError:
        return PolicyDeniedError(
            f"响应体超过上限: {url}",
            adapter="api",
            detail=(
                f"至少 {actual} 字节，上限 {limit} 字节。"
                "下载大文件请走 ssh.download；确需放宽可在 security.api_max_response_bytes 调整"
            ),
        )

    # -- 目标校验 -----------------------------------------------------------
    def _assert_target_allowed(self, url: str, args: dict[str, Any]) -> None:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise PolicyDeniedError(
                f"api 目标协议不受支持: {url}",
                adapter=self.name,
                detail="只允许 http/https；file://、gopher:// 这类协议是 SSRF 的常用跳板",
            )

        host = parsed.hostname or ""
        if not host:
            raise ConfigError(f"api 目标缺少主机名: {url}", adapter=self.name)

        if args.get("allow_any_host"):
            # 和 SSH 一致：单个用例不得自行绕过策略，必须由配置层统一开启
            if not self.config.api_allow_any_host():
                raise PolicyDeniedError(
                    "用例请求 allow_any_host，但配置未开启",
                    adapter=self.name,
                    detail="确需豁免时，在 mtp_config.yaml 设置 security.api_allow_any_host: true",
                )
            return

        allowed = self.config.api_allow_hosts()
        if allowed is not None:
            candidate = host.lower()
            for pattern in allowed:
                if fnmatch.fnmatchcase(candidate, str(pattern).lower()):
                    return
            raise PolicyDeniedError(
                f"api 目标不在白名单: {host}",
                adapter=self.name,
                detail=(
                    f"允许: {', '.join(allowed) or '(空，等于全部拒绝)'}；"
                    "在 mtp_config.yaml 的 security.api_allow_hosts 中放行目标"
                ),
            )

        # 未配白名单时的兜底：挡掉「谁都不会主动去测」的地址
        self._reject_dangerous_ip(host)

    @staticmethod
    def _reject_dangerous_ip(host: str) -> None:
        """挡掉云元数据 / 链路本地地址，包括**解析后**命中它们的域名。

        只比对「字符串是不是内网地址」是不够的：`metadata.internal` 这类域名
        解析到 169.254.169.254，一样能把请求打进元数据服务。
        """
        addrs: list[str] = []
        try:
            addrs = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addrs = [info[4][0] for info in socket.getaddrinfo(host, None)]
            except OSError:
                # 解析不了就交还给请求阶段：那里会给出更准确的错误，
                # 这里不假装成策略问题。
                return

        for raw in addrs:
            candidate = str(raw).split("%")[0]
            try:
                ip = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            for network in _BLOCKED_NETWORKS:
                if ip.version == network.version and ip in network:
                    raise PolicyDeniedError(
                        f"api 目标命中禁止地址段: {host} -> {ip}",
                        adapter="api",
                        detail=(
                            f"{network} 通常是云元数据或链路本地地址，不是被测服务。"
                            "确要访问请在 security.api_allow_hosts 中显式放行"
                        ),
                    )

    # -- 辅助 ---------------------------------------------------------------
    def _proxies_for(self, url: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """决定这次请求是否绕过 HTTP(S)_PROXY。

        开发机上通常挂着代理，本地/内网的被测服务直接被代理接管会拿到 502。
        规则：命中 `security.http_no_proxy` 就绕过；用例可显式
        `use_proxy: true` 强制走代理。
        """
        if args.get("use_proxy") is True:
            return None

        import fnmatch
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if not host:
            return None
        patterns = [str(p).lower() for p in (self.config.security.get("http_no_proxy") or [])]
        if any(fnmatch.fnmatch(host, pattern) for pattern in patterns):
            # None 表示「对这个协议不使用代理」，与 requests 的语义一致
            return {"http": None, "https": None}
        return None

    @staticmethod
    def _resolve_url(args: dict[str, Any], context: StepContext) -> str:
        url = args.get("url")
        path = args.get("path")
        base = args.get("base_url") or (context.env or {}).get("base_url")

        if url and path:
            raise ConfigError("api 步骤不能同时给 url 和 path", adapter="api")
        if path:
            if not base:
                raise ConfigError(
                    "api 步骤给了 path 但找不到 base_url",
                    adapter="api",
                    detail="在 environment.base_url 中声明，或在 args.base_url 直接给全",
                )
            return urljoin(str(base).rstrip("/") + "/", str(path).lstrip("/"))
        if url:
            return str(url)
        raise ConfigError("api 步骤需要 url 或 path", adapter="api")

    def _prepare_files(self, files: Any, context: StepContext) -> list[tuple[str, Any]] | None:
        """把用例里的 `files` 转成 requests 的 multipart 参数。

        上传路径必须落在 `security.api_upload_roots` 之内：否则用例可以指定任意
        本地文件（例如 `~/.ssh/id_rsa`）把它传出去。
        """
        if not files:
            return None
        roots = self.config.api_upload_roots()
        prepared: list[tuple[str, Any]] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field") or entry.get("name") or "file")
            raw = entry.get("path")
            if not raw:
                raise ConfigError("files 条目缺少 path", adapter="api")
            path = _resolve_within(Path(str(raw)), roots, reason="上传文件")
            if not path.exists():
                raise ConfigError(f"上传文件不存在: {path}", adapter="api")
            if not path.is_file():
                raise ConfigError(f"上传路径不是普通文件: {path}", adapter="api")
            prepared.append((field, (path.name, path.read_bytes())))
        return prepared or None

    @staticmethod
    def _summarize(method: str, url: str, status: int, duration_ms: int) -> str:
        # 摘要里只有方法、URL、状态码、耗时——不含 header / body
        return f"{method} {url} -> {status}（{duration_ms}ms）"
