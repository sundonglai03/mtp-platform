"""mtp 命令行入口（自动化测试系统）。

命令：
- `validate`  用内联 contracts-core 校验用例；
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

from mtp_platform.audit import AuditLog
from mtp_platform.config import load_config
from mtp_platform.contracts.case_validator import load_case, validate_file
from mtp_platform.contracts.results import new_run_id, now_iso
from mtp_platform.engine import TestRunner
from mtp_platform.reporting import (
    append as append_history,
    format_trend,
    status_line,
    write_html,
    write_json,
    write_junit,
    write_office_reports,
)
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
# run
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
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

    run_id = args.run_id or new_run_id()
    started_at = now_iso()

    audit = AuditLog(
        config.artifact_root() / "_audit" / f"{run_id}.jsonl",
        redact_keys=config.redact_keys(),
        placeholder=config.redact_placeholder(),
    )
    audit.run_start(run_id=run_id, cases=[str(c) for c in cases], allow_write=args.allow_write)

    runner = TestRunner(
        config,
        registry=ToolRegistry(config),
        allow_write=args.allow_write,
        artifacts_root=config.artifact_root(),
        audit=audit,
    )
    results = []
    try:
        for path in cases:
            print(f"▶  执行 {path}")
            result = runner.run_case_file(path, run_id=run_id)
            results.append(result)
            mark = {"passed": "PASS", "failed": "FAIL", "error": "ERR "}.get(
                result.status.value, result.status.value.upper()
            )
            print(f"   [{mark}] {result.case_id}  {result.duration_ms}ms")
            failure = result.first_failure()
            if failure and not result.passed:
                if failure.get("kind") == "step":
                    print(f"          失败步骤: {failure.get('step_id')} — {failure.get('summary')}")
                elif failure.get("kind") == "assertion":
                    print(f"          失败断言: {failure.get('id')} — {failure.get('message')}")
                else:
                    print(f"          用例错误: {failure.get('message')}")
            for warning in result.warnings:
                print(f"          ⚠ {warning}")
    finally:
        runner.close()

    finished_at = now_iso()
    out_dir = Path(args.out) if args.out else config.report_dir()

    payload = write_json(
        results,
        out_dir,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        allow_write=args.allow_write,
        config_path=str(config.path),
        filename=f"{run_id}-results.json",
    )
    junit_path = write_junit(results, out_dir, run_id=run_id, filename=f"{run_id}-junit.xml")
    html_path = write_html(payload, out_dir, filename=f"{run_id}-report.html")

    # Office 是附加产物：缺依赖或生成失败只告警，不改变这次 run 的结论
    office: dict = {"xlsx": None, "docx": None, "errors": []}
    if args.office:
        office = write_office_reports(payload, out_dir, filename_prefix=f"{run_id}-report")
        for item in office["errors"]:
            print(f"⚠  {item['kind']} 报告生成失败: {item['error']}", file=sys.stderr)

    # 「最新」指针：方便 CI 固定路径收集
    latest = out_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for src, name in (
        (payload["_written_to"], "results.json"),
        (junit_path, "junit.xml"),
        (html_path, "report.html"),
        (office["xlsx"], "report.xlsx"),
        (office["docx"], "report.docx"),
    ):
        if src:
            (latest / name).write_bytes(Path(src).read_bytes())

    append_history(payload, config.history_file())
    audit.run_end(
        run_id=run_id,
        status="success" if payload["summary"]["success"] else "failure",
        duration_ms=payload["summary"]["duration_ms"],
        summary=payload["summary"],
    )

    print()
    print(status_line(payload))
    print(f"JSON : {payload['_written_to']}")
    print(f"JUnit: {junit_path}")
    print(f"HTML : {html_path}")
    if office["xlsx"]:
        print(f"Excel: {office['xlsx']}")
    if office["docx"]:
        print(f"Word : {office['docx']}")
    if args.office and not (office["xlsx"] and office["docx"]):
        print("（Office 报告不完整，见上面的告警）")

    return 0 if payload["summary"]["success"] else 1


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

    p_validate = _add_config(sub.add_parser("validate", help="校验用例（内联 contracts-core）"))
    p_validate.add_argument("targets", nargs="+")

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

    sub.add_parser("version", help="打印版本")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "validate": cmd_validate,
        "run": cmd_run,
        "trend": cmd_trend,
        "version": cmd_version,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
