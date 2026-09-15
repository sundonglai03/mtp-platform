"""CLI 辅助函数测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mtp_platform.cli import _collect

CASE = "schema_version: 1\nid: X\ntitle: t\nsteps: []\n"


def test_collect_skips_report_artifacts():
    """把 --out 指到用例目录里时，上一轮的 results.json 不能被当成用例。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "case.yaml").write_text(CASE, encoding="utf-8")
        (root / "reports").mkdir()
        (root / "reports" / "20260915T000000-abcd-results.json").write_text(
            "{}", encoding="utf-8"
        )
        (root / "reports" / "results.json").write_text("{}", encoding="utf-8")
        (root / "reports" / "snapshot.json").write_text("{}", encoding="utf-8")
        (root / "latest").mkdir()
        (root / "latest" / "results.json").write_text("{}", encoding="utf-8")

        found = _collect([str(root)])

    # reports/ 整个目录被跳过；只有用例被收进来
    assert sorted(p.name for p in found) == ["case.yaml"]


def test_collect_dedupes_and_handles_missing_targets(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        case = root / "a.yaml"
        case.write_text(CASE, encoding="utf-8")

        found = _collect([str(case), str(case), str(root / "nope.yaml")])

    assert [p.name for p in found] == ["a.yaml"]
    assert "找不到" in capsys.readouterr().err
