"""contracts 内联副本的漂移检测。

contracts-core 在两个系统里各有一份副本，靠 `scripts/sync_contracts.py` 保持同步。
本测试用 `_sync_manifest.json` 校验副本没有被手改（或改后忘了同步）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[1] / "src" / "mtp_platform" / "contracts"
MANIFEST = "_sync_manifest.json"


def _actual() -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(CONTRACTS.iterdir())
        if p.is_file() and p.name != MANIFEST
    }


def test_contracts_copy_matches_manifest():
    manifest = json.loads((CONTRACTS / MANIFEST).read_text(encoding="utf-8"))
    assert _actual() == manifest, (
        "contracts 副本被改动或未同步：请运行 scripts/sync_contracts.py"
    )
