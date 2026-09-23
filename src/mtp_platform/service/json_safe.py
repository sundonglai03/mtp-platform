"""结果落库前的 JSON 序列化兜底。

工具可能返回 Python 对象而不是 JSON 原生值 —— 例如 MySQL 的时间列返回 `datetime`、
聚合列返回 `Decimal`、BLOB 列返回 `bytes`。`json.dumps` 遇到它们会直接抛 `TypeError`，
而这条写入路径**在单用例保护之外**：一个坏值就会终止整个任务。

实测（2026-09-23，10 个用例的 cert 套件）：第 1、2 个用例都成功了，写第 2 个用例结果时
抛 `TypeError: Object of type datetime is not JSON serializable`，任务直接变成 error、
剩下 8 个用例根本没跑。

所以：结果写入一律走这里（`default=str` 兜底 + 保持中文可读）。数据侧的根因在
`tools/mysql.py` 已同时修掉 —— 数据库值会先归一化成 JSON 原生类型，这里只是最后一道网。
"""

from __future__ import annotations

import json
from typing import Any


def dumps(value: Any, **kwargs: Any) -> str:
    """`json.dumps` + `default=str`：未知类型退化成字符串，绝不因为一个值炸掉整个任务。"""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", str)
    return json.dumps(value, **kwargs)
