"""HTML 汇总报告。

目标（reporting/TASK 验收标准）：**能直接定位失败步骤**。
所以版式把「失败摘要」放在最前面，每条失败都给出「步骤 → 断言 → 证据链接」，
不需要人去翻 JSON。

单文件输出（CSS 内联），不依赖任何外部资源，可以直接当 CI artifact 打开。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from jinja2 import Template

# autoescape=True：报告里会渲染步骤输出、响应体等外部文本，必须转义
_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>测试报告 {{ payload.run_id }}</title>
<style>
  :root {
    --bg: #f7f8fa; --panel: #ffffff; --ink: #1f2430; --muted: #646b7a;
    --line: #e2e5ec; --pass: #0f7b3f; --fail: #b3261e; --error: #8a4b00;
    --skip: #5a6273; --accent: #2b5fd9;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--ink);
         font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; }
  .wrap { max-width: 1080px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
  h2 { font-size: 1.1rem; margin: 2rem 0 .75rem; }
  .sub { color: var(--muted); font-size: .85rem; margin-bottom: 1.5rem; }
  .chips { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1.75rem; }
  .chip { background: var(--panel); border: 1px solid var(--line); border-radius: 999px;
          padding: .3rem .8rem; font-size: .82rem; }
  .chip b { font-weight: 600; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
           padding: 1rem 1.1rem; margin-bottom: 1rem; }
  .panel.fail { border-left: 4px solid var(--fail); }
  .panel.error { border-left: 4px solid var(--error); }
  .panel.pass { border-left: 4px solid var(--pass); }
  .panel.cancel { border-left: 4px solid var(--skip); }
  .case-head { display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap; }
  .case-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85rem;
             color: var(--muted); }
  .badge { font-size: .72rem; text-transform: uppercase; letter-spacing: .03em;
           border-radius: 4px; padding: .12rem .45rem; font-weight: 600; }
  .badge.passed { background: #e6f4ea; color: var(--pass); }
  .badge.failed { background: #fdecea; color: var(--fail); }
  .badge.error { background: #fdf1e0; color: var(--error); }
  .badge.cancelled, .badge.skipped { background: #eef0f4; color: var(--skip); }
  table { width: 100%; border-collapse: collapse; margin-top: .75rem; font-size: .85rem; }
  th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid var(--line);
           vertical-align: top; }
  th { color: var(--muted); font-weight: 600; font-size: .78rem; }
  td.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .78rem; }
  .fail-cell { color: var(--fail); }
  pre { background: #f2f4f8; border: 1px solid var(--line); border-radius: 6px;
        padding: .6rem .7rem; overflow-x: auto; font-size: .78rem; margin: .4rem 0 0; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .muted { color: var(--muted); }
  .empty { color: var(--muted); font-style: italic; }
  details summary { cursor: pointer; color: var(--accent); font-size: .82rem; }
</style>
</head>
<body>
<div class="wrap">
  <h1>测试报告</h1>
  <div class="sub">
    run <code>{{ payload.run_id }}</code> ·
    生成于 {{ payload.generated_at }}{% if payload.context.config_path %} ·
    配置 <code>{{ payload.context.config_path }}</code>{% endif %}
    {% if payload.context.allow_write %} · <b>已开启写操作</b>{% endif %}
  </div>

  <div class="chips">
    <span class="chip">用例 <b>{{ payload.summary.cases_total }}</b></span>
    <span class="chip">通过 <b>{{ payload.summary.cases_passed }}</b></span>
    <span class="chip">失败 <b>{{ payload.summary.cases_failed }}</b></span>
    <span class="chip">错误 <b>{{ payload.summary.cases_error }}</b></span>
    <span class="chip">取消 <b>{{ payload.summary.cases_cancelled }}</b></span>
    <span class="chip">断言失败 <b>{{ payload.summary.assertions_failed }}/{{ payload.summary.assertions_total }}</b></span>
    <span class="chip">耗时 <b>{{ payload.summary.duration_ms }} ms</b></span>
  </div>

  {% if failures %}
  <h2>失败定位</h2>
  {% for item in failures %}
  <div class="panel {{ item.kind }}">
    <div class="case-head">
      <span class="badge {{ item.kind }}">{{ item.kind }}</span>
      <b>{{ item.title }}</b>
      <span class="case-id">{{ item.case_id }}</span>
    </div>
    <div class="muted" style="margin-top:.4rem">{{ item.what }}</div>
    {% if item.where %}<div class="mono muted" style="margin-top:.3rem">{{ item.where }}</div>{% endif %}
    {% if item.detail %}<pre>{{ item.detail }}</pre>{% endif %}
    {% if item.evidence %}
    <div class="muted" style="margin-top:.5rem">证据：
      {% for ev in item.evidence %}
      {% if ev.path %}<a href="{{ ev.link }}">{{ ev.kind }} · {{ ev.id }}</a>{% else %}<span>{{ ev.kind }} · {{ ev.id }}</span>{% endif %}
      {% else %}<span class="empty">无</span>{% endfor %}
    </div>
    {% endif %}
  </div>
  {% endfor %}
  {% else %}
  <div class="panel pass"><b>全部通过</b>，没有失败需要定位。</div>
  {% endif %}

  <h2>全部用例</h2>
  {% for case in cases %}
  <div class="panel {{ case.css }}">
    <div class="case-head">
      <span class="badge {{ case.status }}">{{ case.status }}</span>
      <b>{{ case.title }}</b>
      <span class="case-id">{{ case.case_id }}</span>
      <span class="muted">{{ case.duration_ms }} ms</span>
      {% for tag in case.tags %}<span class="chip">{{ tag }}</span>{% endfor %}
    </div>

    {% if case.error %}<pre>{{ case.error }}</pre>{% endif %}
    {% for w in case.warnings %}<div class="muted">⚠ {{ w }}</div>{% endfor %}

    <table>
      <thead><tr><th style="width:34%">步骤</th><th>动作</th><th style="width:8%">状态</th>
      <th style="width:10%">耗时</th><th>摘要</th></tr></thead>
      <tbody>
      {% for step in case.steps %}
        <tr>
          <td class="mono">{{ step.step_id }}<div class="muted">{{ step.phase }}</div></td>
          <td class="mono">{{ step.action }}</td>
          <td class="{{ 'fail-cell' if step.status in ('failed','error') else '' }}">{{ step.status }}</td>
          <td class="mono">{{ step.duration_ms }}ms</td>
          <td>{{ step.summary }}{% if step.error %}<div class="fail-cell">{{ step.error.code }}: {{ step.error.message }}</div>{% endif %}</td>
        </tr>
      {% else %}
        <tr><td colspan="5" class="empty">无步骤</td></tr>
      {% endfor %}
      </tbody>
    </table>

    {% if case.assertions %}
    <details>
      <summary>断言（{{ case.assertions | length }} 条，失败 {{ case.assertions_failed }}）</summary>
      <table>
        <thead><tr><th style="width:22%">ID</th><th style="width:16%">类型</th><th style="width:8%">结果</th>
        <th>期望 / 实际 / 说明</th></tr></thead>
        <tbody>
        {% for a in case.assertions %}
          <tr>
            <td class="mono">{{ a.id }}</td>
            <td class="mono">{{ a.type }}</td>
            <td class="{{ '' if a.passed else 'fail-cell' }}">{{ '通过' if a.passed else '失败' }}</td>
            <td>
              <div>期望 <code>{{ a.expected }}</code></div>
              <div>实际 <code>{{ a.actual }}</code></div>
              <div class="muted">{{ a.message }}</div>
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </details>
    {% endif %}

    {% if case.evidence %}
    <details>
      <summary>证据（{{ case.evidence | length }} 份）</summary>
      <div class="muted" style="margin-top:.4rem">
      {% for ev in case.evidence %}
        {% if ev.link %}<a href="{{ ev.link }}">{{ ev.kind }} · {{ ev.id }}</a>
        {% else %}<span>{{ ev.kind }} · {{ ev.id }}</span>{% endif %}
        <span class="muted">({{ ev.step_id }}, {{ ev.bytes }}B)</span>{% if not loop.last %} · {% endif %}
      {% endfor %}
      </div>
    </details>
    {% endif %}
  </div>
  {% endfor %}
</div>
</body>
</html>
""",
    autoescape=True,
)


def _link(path: str, report_dir: Path) -> str:
    """把证据的相对路径换算成从报告文件出发的相对链接。"""
    if not path:
        return ""
    absolute = (Path(os.getcwd()) / path).resolve() if not Path(path).is_absolute() else Path(path)
    try:
        return os.path.relpath(absolute, report_dir.resolve())
    except ValueError:
        return path


def _preview(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def build_failures(payload: dict[str, Any], report_dir: Path) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for case in payload.get("cases", []):
        status = case.get("status")
        if status == "passed":
            continue

        failure = _first_failure(case)
        evidence = [
            {**ev, "link": _link(ev.get("path", ""), report_dir)}
            for ev in (case.get("evidence") or [])
        ]
        if failure["kind"] == "step":
            what = f"步骤 {failure['step_id']} 失败：{failure.get('summary', '')}"
            where = f"{failure.get('phase')} · {failure.get('action')} · 尝试 {failure.get('attempts')} 次"
            err = failure.get("error") or {}
            detail = f"{err.get('code', '')}: {err.get('message', '')}"
            if err.get("detail"):
                detail += f"\n{_preview(err['detail'], 1200)}"
        elif failure["kind"] == "assertion":
            what = f"断言 {failure.get('id')} 失败：{failure.get('message', '')}"
            where = failure.get("type", "")
            detail = f"期望: {_preview(failure.get('expected'))}\n实际: {_preview(failure.get('actual'))}"
        else:
            err = case.get("error") or {}
            what = f"用例执行错误：{err.get('message', status)}"
            where = err.get("code", "")
            detail = _preview(err.get("detail", ""), 1200)

        failures.append(
            {
                "kind": "error" if status == "error" else ("cancel" if status == "cancelled" else "fail"),
                "case_id": case.get("case_id"),
                "title": case.get("title") or case.get("case_id"),
                "what": what,
                "where": where,
                "detail": detail,
                "evidence": evidence,
            }
        )
    return failures


def _first_failure(case: dict[str, Any]) -> dict[str, Any]:
    for step in case.get("steps", []):
        if step.get("status") in {"failed", "error"}:
            return {"kind": "step", **step}
    for assertion in case.get("assertions", []):
        if not assertion.get("passed"):
            return {"kind": "assertion", **assertion}
    return {"kind": "case"}


def write_html(
    payload: dict[str, Any],
    out_dir: str | Path,
    *,
    filename: str = "report.html",
) -> Path:
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename

    cases: list[dict[str, Any]] = []
    for case in payload.get("cases", []):
        enriched = dict(case)
        status = case.get("status", "")
        enriched["css"] = {
            "passed": "pass",
            "failed": "fail",
            "error": "error",
        }.get(status, "cancel")
        enriched["assertions_failed"] = sum(
            1 for a in case.get("assertions", []) if not a.get("passed")
        )
        enriched["error"] = _preview((case.get("error") or {}).get("message", "")) or None
        enriched["evidence"] = [
            {**ev, "link": _link(ev.get("path", ""), directory)}
            for ev in (case.get("evidence") or [])
        ]
        cases.append(enriched)

    rendered = _TEMPLATE.render(
        payload=payload,
        cases=cases,
        failures=build_failures(payload, directory),
    )
    target.write_text(rendered, encoding="utf-8")
    return target
