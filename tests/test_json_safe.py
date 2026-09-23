"""结果落库的 JSON 兜底与数据库值归一化。

对应 2026-09-23 的事故：cert 套件 10 个用例，前两个都成功，写结果时抛
`TypeError: Object of type datetime is not JSON serializable` —— 异常冒到任务边界，
整个任务变 error、剩下 8 个用例根本没跑。根因是 MySQL 的时间列原样进了步骤数据。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from mtp_platform.service.json_safe import dumps
from mtp_platform.tools.mysql import _json_safe


def test_dumps_survives_values_json_cannot_handle():
    """落库路径上的任何怪值都不该再抛异常（否则又是一个用例毁整个任务）。"""
    payload = {
        "when": datetime(2026, 9, 23, 11, 39, 7),
        "amount": Decimal("1.50"),
        "raw": b"ok",
        "obj": object(),
    }

    text = dumps(payload)

    assert "2026-09-23 11:39:07" in text
    # bytes 走 default=str 变成 repr；重点是**不再抛异常**（数据库侧已提前归一化成文本）
    assert json.loads(text)["raw"] == str(b"ok")


def test_mysql_rows_are_normalised_to_json_native_types():
    rows = [
        {
            "id": 1,
            "created_at": datetime(2026, 9, 23, 11, 39, 7),
            "day": date(2026, 9, 23),
            "amount": Decimal("3"),
            "ratio": Decimal("1.50"),
            "elapsed": timedelta(seconds=5),
            "blob": b"\xff\xfe",
        }
    ]

    safe = _json_safe(rows)

    assert safe == [
        {
            "id": 1,
            "created_at": "2026-09-23T11:39:07",
            "day": "2026-09-23",
            "amount": 3,          # 整数 Decimal -> int
            "ratio": 1.5,         # 小数 Decimal -> float
            "elapsed": "0:00:05",
            "blob": repr(b"\xff\xfe"),  # 非 utf-8 退化成 repr
        }
    ]
    # 这就是以前会炸的地方：归一化后能直接进 json.dumps
    assert json.dumps(safe)


def test_progress_payload_with_db_datetime_is_persistable():
    """复刻事故现场：用例结果里带数据库时间列时，jobs.progress() 那一串写入不能炸。"""
    payload = [
        {
            "case_id": "cert-01-clean-and-apply",
            "status": "passed",
            "steps": [
                {
                    "step_id": "db-query",
                    "data": {"rows": [{"id": 1, "updated_at": datetime(2026, 9, 23, 11, 39, 7)}]},
                }
            ],
        }
    ]

    restored = json.loads(dumps(payload))

    assert restored[0]["steps"][0]["data"]["rows"][0]["updated_at"] == "2026-09-23 11:39:07"


def test_mysql_normalisation_keeps_plain_values_untouched():
    assert _json_safe(None) is None
    assert _json_safe("x") == "x"
    assert _json_safe(7) == 7
    assert _json_safe(1.5) == 1.5
    assert _json_safe(True) is True
    assert _json_safe({"a": [1, {"b": time(11, 39)}]}) == {"a": [1, {"b": "11:39:00"}]}
