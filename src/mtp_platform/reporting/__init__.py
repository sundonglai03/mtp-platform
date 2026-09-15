"""报告层：同一份 payload 派生 JSON / JUnit / HTML。

- `report_payload.build_payload`：把 CaseResult 列表归一成报告 payload（唯一事实来源）；
- `json_reporter` / `junit_reporter` / `html_reporter`：只消费 payload，不重算结论；
- `history`：把每次运行追加进 history.jsonl，并给出趋势；
- `office_reporter`：xlsx / docx（**原生 openpyxl / python-docx，不走 officecli**）。

Office 报告是附加产物：缺依赖或生成失败只告警，不影响 run 结论。
"""

from mtp_platform.reporting.history import append, format_trend
from mtp_platform.reporting.html_reporter import write_html
from mtp_platform.reporting.json_reporter import write_json
from mtp_platform.reporting.junit_reporter import write_junit
from mtp_platform.reporting.office_reporter import write_office_reports
from mtp_platform.reporting.report_payload import build_payload, status_line

__all__ = [
    "build_payload",
    "status_line",
    "write_json",
    "write_junit",
    "write_html",
    "write_office_reports",
    "append",
    "format_trend",
]
