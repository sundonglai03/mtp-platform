"""报告层：同一份 payload 派生 JSON / JUnit / HTML。

- `report_payload.build_payload`：把 CaseResult 列表归一成报告 payload（唯一事实来源）；
- `json_reporter` / `junit_reporter` / `html_reporter`：只消费 payload，不重算结论；
- `history`：把每次运行追加进 history.jsonl，并给出趋势。

Office（xlsx/docx）报告随后补（需要直连 office 实现）。
"""

from mtp_platform.reporting.history import append, format_trend
from mtp_platform.reporting.html_reporter import write_html
from mtp_platform.reporting.json_reporter import write_json
from mtp_platform.reporting.junit_reporter import write_junit
from mtp_platform.reporting.report_payload import build_payload, status_line

__all__ = [
    "build_payload",
    "status_line",
    "write_json",
    "write_junit",
    "write_html",
    "append",
    "format_trend",
]
