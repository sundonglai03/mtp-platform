"""证据存储。

统一管理截图、页面快照、Console/Network、SSH 输出、MySQL 结果、HTTP 响应。

三条硬约束（对应 evidence/TASK 的验收标准）：

1. **关联可追溯**：每份证据都带 `run_id / case_id / step_id`，所以失败用例能
   一路定位到具体步骤；
2. **结果只留元数据**：所有内容均落盘；SQLite/API 只保存路径、MIME 类型和大小，
   不嵌入 Base64 或报告内容；
3. **落盘前脱敏**：所有文本/JSON 在写文件之前先过 `redact`，
   证据目录里不允许出现凭据原文。

目录结构::

    artifacts/runs/<run_id>/evidence/<case_id>/<step_id>/<name>
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtp_contracts.redaction import DEFAULT_PLACEHOLDER, DEFAULT_REDACT_KEYS, SecretRegistry, redact, redact_text

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str, fallback: str = "item") -> str:
    """把任意标识符压成安全的路径片段（同时挡住 ../ 之类的穿越）。"""
    cleaned = _SAFE.sub("-", str(name).strip()).strip("-.")
    return cleaned[:80] or fallback


@dataclass
class EvidenceRef:
    id: str
    kind: str
    run_id: str
    case_id: str
    step_id: str
    path: str = ""
    bytes: int = 0
    sha256: str = ""
    summary: str = ""
    mime_type: str = "application/octet-stream"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "step_id": self.step_id,
            "bytes": self.bytes,
            "mime_type": self.mime_type,
            "summary": self.summary,
        }
        if self.path:
            out["path"] = self.path
        if self.sha256:
            out["sha256"] = self.sha256
        return out


class EvidenceStore:
    def __init__(
        self,
        root: str | Path,
        run_id: str,
        *,
        registry: SecretRegistry | None = None,
        redact_keys: list[str] | None = None,
        placeholder: str = DEFAULT_PLACEHOLDER,
    ) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self.registry = registry or SecretRegistry()
        self.redact_keys = list(redact_keys or DEFAULT_REDACT_KEYS)
        self.placeholder = placeholder
        self._refs: list[EvidenceRef] = []
        self._counter = 0

    # -- 内部 ---------------------------------------------------------------
    def _next_id(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}-{self._counter:04d}"

    def _dir(self, case_id: str, step_id: str) -> Path:
        path = self.root / "evidence" / _safe(case_id, "case") / _safe(step_id, "step")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def _sanitize_text(self, text: str) -> str:
        scrubbed = redact_text(text, self.registry)
        return redact(
            scrubbed, keys=self.redact_keys, placeholder=self.placeholder, registry=self.registry
        )

    def _add(self, ref: EvidenceRef) -> EvidenceRef:
        self._refs.append(ref)
        return ref

    # -- 写入 ---------------------------------------------------------------
    def save_text(
        self,
        kind: str,
        case_id: str,
        step_id: str,
        name: str,
        text: str,
        *,
        ext: str = ".txt",
        summary: str = "",
    ) -> EvidenceRef:
        cleaned = self._sanitize_text(text or "")
        raw = cleaned.encode("utf-8")
        ref = EvidenceRef(
            id=self._next_id(kind),
            kind=kind,
            run_id=self.run_id,
            case_id=case_id,
            step_id=step_id,
            bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest()[:16],
            summary=summary or f"{kind} {len(raw)} 字节",
        )
        target = self._dir(case_id, step_id) / f"{_safe(name)}{ext}"
        target.write_bytes(raw)
        ref.path = self._relative(target)
        ref.mime_type = mimetypes.guess_type(target.name)[0] or "text/plain"
        return self._add(ref)

    def save_json(
        self,
        kind: str,
        case_id: str,
        step_id: str,
        name: str,
        payload: Any,
        *,
        summary: str = "",
    ) -> EvidenceRef:
        safe_payload = redact(
            payload, keys=self.redact_keys, placeholder=self.placeholder, registry=self.registry
        )
        text = json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str)
        return self.save_text(
            kind, case_id, step_id, name, text, ext=".json", summary=summary or f"{kind} 记录"
        )

    def save_bytes(
        self,
        kind: str,
        case_id: str,
        step_id: str,
        name: str,
        data: bytes,
        *,
        ext: str = ".bin",
        summary: str = "",
    ) -> EvidenceRef:
        raw = data or b""
        target = self._dir(case_id, step_id) / f"{_safe(name)}{ext}"
        target.write_bytes(raw)
        ref = EvidenceRef(
            id=self._next_id(kind),
            kind=kind,
            run_id=self.run_id,
            case_id=case_id,
            step_id=step_id,
            path=self._relative(target),
            bytes=len(raw),
            mime_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            sha256=hashlib.sha256(raw).hexdigest()[:16],
            summary=summary or f"{kind} {len(raw)} 字节",
        )
        return self._add(ref)

    def save_base64(
        self,
        kind: str,
        case_id: str,
        step_id: str,
        name: str,
        data: str,
        *,
        ext: str = ".png",
        summary: str = "",
    ) -> EvidenceRef:
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception:  # noqa: BLE001
            raw = b""
        return self.save_bytes(
            kind, case_id, step_id, name, raw, ext=ext, summary=summary or f"{kind} 图像"
        )

    def record(self, kind: str, case_id: str, step_id: str, summary: str, **extra: Any) -> EvidenceRef:
        """只登记一条引用（内容已在别处保存）。"""
        ref = EvidenceRef(
            id=self._next_id(kind),
            kind=kind,
            run_id=self.run_id,
            case_id=case_id,
            step_id=step_id,
            summary=summary,
        )
        for key, value in extra.items():
            if hasattr(ref, key):
                setattr(ref, key, value)
        return self._add(ref)

    # -- 查询 ---------------------------------------------------------------
    def refs(self) -> list[EvidenceRef]:
        return list(self._refs)

    def refs_for(self, *, case_id: str | None = None, step_id: str | None = None) -> list[EvidenceRef]:
        return [
            r
            for r in self._refs
            if (case_id is None or r.case_id == case_id)
            and (step_id is None or r.step_id == step_id)
        ]

    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._refs]
