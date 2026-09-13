"""mtp 命令行入口（自动化测试系统）。

当前提供 `validate`（用内联 contracts-core 校验用例）。
engine / direct tools / run / report 等命令随后续阶段补上。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mtp_platform.contracts.case_validator import validate_file


def _collect(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        path = Path(target)
        if path.is_dir():
            for pattern in ("*.yaml", "*.yml", "*.json"):
                files.extend(sorted(path.rglob(pattern)))
        elif path.exists():
            files.append(path)
        else:
            print(f"!! 找不到: {target}", file=sys.stderr)

    seen: set[str] = set()
    unique: list[Path] = []
    for f in files:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def cmd_validate(args: argparse.Namespace) -> int:
    cases = _collect(args.targets)
    if not cases:
        print("!! 没有找到任何用例文件", file=sys.stderr)
        return 2

    bad = 0
    for path in cases:
        result = validate_file(path)
        if result.ok:
            print(f"[OK]   {path}")
        else:
            bad += 1
            print(f"[FAIL] {path}")
            for message in result.messages():
                print(f"         - {message}")
    print(f"\n共 {len(cases)} 个用例，{len(cases) - bad} 通过校验，{bad} 个不合法")
    return 1 if bad else 0


def cmd_version(_args: argparse.Namespace) -> int:
    from mtp_platform import __version__

    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mtp", description="自动化测试系统（mtp-platform）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验用例（使用内联 contracts-core）")
    p_validate.add_argument("targets", nargs="+")

    sub.add_parser("version", help="打印版本")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"validate": cmd_validate, "version": cmd_version}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
