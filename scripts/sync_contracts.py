"""把 contracts-core 内联副本同步到两个系统项目。

- **唯一事实来源**：`mtp-platform` 内的 `mtp_platform/contracts/`（要改 contracts 就改这里）。
- 本脚本把它逐字节复制到 `mtp-contracts-mcp` 的 `mtp_contracts_mcp/contracts/`，
  并重新生成两侧的 `_sync_manifest.json`。

为什么能逐字节一致：副本内部全部使用**相对导入**（`from .errors import ...`），
不含包名，所以在两个不同父包下内容相同，可直接哈希比对。
`tests/test_contracts_integrity.py` 会用 manifest 检测手改/未同步造成的漂移。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

STUDY = Path(__file__).resolve().parents[2]

MASTER = STUDY / "mtp-platform" / "src" / "mtp_platform" / "contracts"
SLAVE = STUDY / "mtp-contracts-mcp" / "src" / "mtp_contracts_mcp" / "contracts"

MANIFEST = "_sync_manifest.json"


def _manifest_of(directory: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
        if p.is_file() and p.name != MANIFEST
    }


def _write_manifest(directory: Path) -> dict[str, str]:
    manifest = _manifest_of(directory)
    (directory / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    if not MASTER.is_dir():
        print(f"找不到源目录: {MASTER}", file=sys.stderr)
        return 2

    if SLAVE.is_dir():
        shutil.rmtree(SLAVE)
    SLAVE.mkdir(parents=True)

    for item in sorted(MASTER.iterdir()):
        if item.is_file() and item.name != MANIFEST:
            shutil.copy2(item, SLAVE / item.name)

    master = _write_manifest(MASTER)
    slave = _write_manifest(SLAVE)

    same = master == slave
    print(f"同步 {len(master)} 个文件: {MASTER} -> {SLAVE}")
    print("两侧一致:", same)
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
