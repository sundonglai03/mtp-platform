"""断言目录 vs 引擎实现：任一侧漂移都要在这条测试里变红。

为什么需要它：动作/断言这类「说明与实现」的漂移很难靠人记住 —— 目录里写了一种断言而引擎
没实现（或反过来：实现了却没进说明），只有真跑到那一步才会暴露。这里用源码扫描把两边钉住：

- 引擎实现的 `_assert_<type>` 必须与 core 的断言目录一一对应；
- 目录里声明的 `args.*` 字段，实现必须真的读它。
"""

from __future__ import annotations

import re
from pathlib import Path

from mtp_contracts.assertion_catalog import ASSERTIONS, known_types

ASSERTIONS_SRC = (
    Path(__file__).resolve().parent.parent / "src" / "mtp_platform" / "engine" / "assertions.py"
)


def _source() -> str:
    return ASSERTIONS_SRC.read_text(encoding="utf-8")


def implemented_types() -> set[str]:
    """引擎真正实现的断言类型（all / any 共用 `_assert_group`）。"""
    handlers = set(re.findall(r"def _assert_([a-z_]+)\(", _source()))
    types = {name for name in handlers if name != "group"}
    if "group" in handlers:
        types |= {"all", "any"}
    return types


def _handler_body(assertion_type: str) -> str:
    handler = "group" if assertion_type in {"all", "any"} else assertion_type
    match = re.search(
        rf"def _assert_{handler}\(.*?(?=\n    def |\n    @|\Z)", _source(), flags=re.S
    )
    assert match, f"找不到 _assert_{handler} 的实现"
    return match.group(0)


def test_every_catalog_assertion_type_is_implemented_and_vice_versa():
    assert implemented_types() == set(known_types()), (
        "断言目录与引擎实现不一致：多声明会变成「校验通过但断言不支持」，"
        "漏声明则是「实现了却没写进说明」，agent 写不出来"
    )


def test_declared_args_fields_are_actually_read_by_the_handler():
    for assertion_type in known_types():
        spec = ASSERTIONS[assertion_type]
        body = _handler_body(assertion_type)
        for field_name in (*spec.requires, *spec.optional):
            if not field_name.startswith("args."):
                continue
            key = field_name.split(".", 1)[1]
            assert re.search(rf'["\']{re.escape(key)}["\']', body), (
                f"断言目录说 {assertion_type} 支持 {field_name}，但实现里没读到 {key!r}"
            )
