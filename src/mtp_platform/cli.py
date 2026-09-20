"""mtp 命令行入口（自动化测试系统）。

命令：
- `validate`  用独立的 contracts-core 包校验用例；
- `run`       注入 ToolRegistry（直连工具）真跑用例，并产出 JSON / JUnit / HTML 报告；
- `trend`     读取 history.jsonl 看趋势；
- `version`   打印版本。

Office（xlsx/docx）报告待直连 office 实现补齐。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from mtp_contracts.case_validator import load_case, validate_file

from mtp_platform.config import load_config
from mtp_platform.reporting import format_trend
from mtp_platform.service.executor import RunExecutor, RunRequest, format_outcome
from mtp_platform.tools.registry import ToolRegistry

# 收集用例时跳过的目录/文件名：这些是**平台自己的产物**，不是用例。
# 不跳过的话，把 --out 指到用例目录里时，上一轮的 results.json 会被当用例再跑一遍。
_SKIP_DIRS = {"reports", "latest", "_audit", "_toolcwd", "__pycache__", ".git"}
_SKIP_JSON_RE = re.compile(r"(^results\.json$|^junit\.xml$|-results\.json$|-junit\.xml$)")


def _collect(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        path = Path(target)
        if path.is_dir():
            for pattern in ("*.yaml", "*.yml", "*.json"):
                for candidate in sorted(path.rglob(pattern)):
                    if any(part in _SKIP_DIRS for part in candidate.parts):
                        continue
                    if _SKIP_JSON_RE.search(candidate.name):
                        continue
                    files.append(candidate)
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


def _has_tag(case_path: Path, tag: str) -> bool:
    try:
        case = load_case(case_path)
    except Exception:  # noqa: BLE001
        return False
    return tag in (case.get("tags") or [])


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    """体检：配置解析、路径问题、各直连工具是否可用。"""
    config = load_config(args.config)
    print(f"配置文件: {config.path}")
    print(f"项目根  : {config.base_dir}")
    print(f"证据目录: {config.artifact_root()}")
    print(f"报告目录: {config.report_dir()}")
    print(f"历史文件: {config.history_file()}")

    print("\n配置路径检查:")
    problems = config.path_problems()
    if problems:
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("  - 全部通过")

    registry = ToolRegistry(config)
    health: dict[str, Any] = {}
    try:
        print("\n直连工具可用性:")
        health = registry.health()
        for name, status in sorted(health.items()):
            if status is True:
                print(f"  - {name:<11} OK")
            elif status is False:
                print(f"  - {name:<11} FAIL  不可用（通常是没有装可选依赖，见 README 的 extras）")
            else:
                print(f"  - {name:<11} FAIL  {status}")
    finally:
        registry.close_all()

    unhealthy = [name for name, status in health.items() if status is not True]
    return 1 if problems or unhealthy else 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    # Preserve the CLI's fail-fast configuration check before target collection.
    load_config(args.config)
    cases = _collect(args.targets)
    if not cases:
        print("!! 没有找到任何用例文件", file=sys.stderr)
        return 2

    if args.tag:
        cases = [c for c in cases if _has_tag(c, args.tag)]
        if not cases:
            print(f"!! 没有用例带标签 {args.tag}", file=sys.stderr)
            return 2

    if args.allow_write:
        print("⚠  已开启 --allow-write：用例中的写操作（insert/update/delete）会被放行")

    def emit(message: str, is_error: bool) -> None:
        print(message, file=sys.stderr if is_error else sys.stdout)

    outcome = RunExecutor().execute(
        RunRequest(
            case_paths=cases,
            config_path=args.config,
            allow_write=args.allow_write,
            office=args.office,
            run_id=args.run_id,
            output_dir=Path(args.out) if args.out else None,
        ),
        emit=emit,
    )
    for line in format_outcome(outcome, office_requested=args.office):
        print(line)
    return outcome.exit_code


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the authenticated HTTP service."""
    import uvicorn

    from mtp_platform.web.app import create_app

    uvicorn.run(create_app(config_path=args.config), host=args.host, port=args.port)
    return 0


# ---------------------------------------------------------------------------
# trend / version
# ---------------------------------------------------------------------------
def cmd_trend(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(format_trend(config.history_file(), limit=args.limit))
    return 0


def cmd_version(_args: argparse.Namespace) -> int:
    from mtp_platform import __version__

    print(__version__)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def _add_config(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument(
        "--config",
        help="配置文件路径（默认 MTP_CONFIG > mtp_config.local.yaml > mtp_config.yaml）",
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mtp", description="自动化测试系统（mtp-platform）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = _add_config(sub.add_parser("validate", help="校验用例（contracts-core）"))
    p_validate.add_argument("targets", nargs="+")

    _add_config(sub.add_parser("doctor", help="体检：配置路径 + 直连工具可用性"))

    p_run = _add_config(sub.add_parser("run", help="执行用例并生成报告"))
    p_run.add_argument("targets", nargs="+")
    p_run.add_argument(
        "--allow-write", action="store_true", help="放行写操作（MySQL insert/update/delete）"
    )
    p_run.add_argument("--tag", help="只跑带该标签的用例")
    p_run.add_argument("--out", help="报告输出目录（默认 config.report_dir()）")
    p_run.add_argument("--run-id", help="指定 run_id（默认自动生成）")
    p_run.add_argument(
        "--office", action="store_true", help="额外生成 Excel / Word 报告（需 office extra）"
    )

    p_trend = _add_config(sub.add_parser("trend", help="历史趋势"))
    p_trend.add_argument("--limit", type=int, default=10)

    p_serve = _add_config(sub.add_parser("serve", help="启动 HTTP 管理服务"))
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    sub.add_parser("version", help="打印版本")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "validate": cmd_validate,
        "doctor": cmd_doctor,
        "run": cmd_run,
        "trend": cmd_trend,
        "serve": cmd_serve,
        "version": cmd_version,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
