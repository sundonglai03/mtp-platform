"""用例校验器。

两道校验，都在**执行之前**完成（非法用例不能进入执行器）：

1. **结构校验**：JSON Schema（`case_schema.json`），错误信息带字段路径，
   形如 `steps[2].action: 'navigate' does not match '^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$'`。
2. **语义校验**：
   - 步骤/断言 id 在同一用例内唯一；
   - `{{ ... }}` 引用必须在已声明的命名空间内（未定义的步骤/变量直接报错，
     带路径），避免运行时才发现拼错；
   - **禁止明文凭据**：敏感字段只允许 `{{ secrets.xxx }}`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import CaseValidationError, ConfigError
from .variables import (
    NAMESPACES,
    PATH_RE,
    STATIC_KEYS,
    TEMPLATE_RE,
    iter_references,
)

SCHEMA_PATH = Path(__file__).resolve().parent / "case_schema.json"

SUPPORTED_SCHEMA_VERSIONS = (1,)

PHASES = ("preconditions", "steps", "postconditions")


@dataclass
class ValidationIssue:
    path: str
    message: str
    kind: str = "schema"

    def render(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


@dataclass
class ValidationResult:
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def messages(self) -> list[str]:
        return [i.render() for i in self.issues]


_schema_cache: dict[str, Any] = {}


def load_schema() -> dict[str, Any]:
    if "schema" not in _schema_cache:
        with SCHEMA_PATH.open("r", encoding="utf-8") as fh:
            _schema_cache["schema"] = json.load(fh)
    return _schema_cache["schema"]


def _get_validator():
    """缓存 Draft202012Validator：构造一次约 2s，逐用例重建会把测试拖成分钟级。"""
    if "validator" not in _schema_cache:
        import jsonschema

        _schema_cache["validator"] = jsonschema.Draft202012Validator(load_schema())
    return _schema_cache["validator"]


def _strip_internal(case: dict[str, Any]) -> dict[str, Any]:
    """去掉平台自己挂上的 `_` 前缀键（如 `_source`），它们不属于用例 schema。"""
    return {k: v for k, v in case.items() if not str(k).startswith("_")}


def load_case(path: str | Path) -> dict[str, Any]:
    """读 YAML/JSON 用例文件。"""
    target = Path(path)
    if not target.exists():
        raise ConfigError(f"用例文件不存在: {target}")

    with target.open("r", encoding="utf-8") as fh:
        if target.suffix.lower() == ".json":
            data = json.load(fh)
        else:
            data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise CaseValidationError(
            f"用例根节点必须是映射: {target}",
            detail=f"实际类型: {type(data).__name__}",
        )
    data.setdefault("_source", str(target))
    return data


def validate_case(case: dict[str, Any], *, source: str = "") -> ValidationResult:
    """只校验不抛错，返回所有问题。"""
    issues: list[ValidationIssue] = []

    validator = _get_validator()
    clean = _strip_internal(case)
    for error in sorted(validator.iter_errors(clean), key=lambda e: list(e.absolute_path)):
        base = _render_path(error.absolute_path)
        # `required` 报在父对象上，路径要补到具体缺失的字段，否则用户只看到 "(root)"
        if error.validator == "required":
            missing = [
                str(name)
                for name in (error.validator_value or [])
                if isinstance(error.instance, dict) and name not in error.instance
            ]
            for name in missing or ["?"]:
                path = f"{base}.{name}" if base != "(root)" else name
                issues.append(
                    ValidationIssue(path=path, message="缺少必填字段", kind="schema")
                )
            continue
        issues.append(ValidationIssue(path=base, message=error.message, kind="schema"))

    # 结构不过关时，语义校验的输入不可信，直接返回
    if issues:
        return ValidationResult(ok=False, issues=issues)

    clean = _strip_internal(case)
    issues.extend(_check_schema_version(clean))
    issues.extend(_check_unique_ids(clean))
    issues.extend(_check_variable_references(clean))
    issues.extend(_check_plaintext_secrets(clean))

    return ValidationResult(ok=not issues, issues=issues)


def require_valid(case: dict[str, Any], *, source: str = "") -> dict[str, Any]:
    """校验失败即抛 `CaseValidationError`（消息里带全部字段路径）。"""
    result = validate_case(case, source=source)
    if not result.ok:
        raise CaseValidationError(
            f"用例不合法（{len(result.issues)} 个问题）",
            detail="\n".join(f"  - {m}" for m in result.messages()),
            extra={"issues": [i.render() for i in result.issues]},
        )
    return case


# ---------------------------------------------------------------------------
# 语义校验
# ---------------------------------------------------------------------------
def _render_path(parts: Any) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "(root)"


def iter_steps(case: dict[str, Any]):
    """按执行顺序产出 (phase, index, step)。fixtures 先于前置条件执行。"""
    for index, step in enumerate(case.get("fixtures") or []):
        yield "fixtures", index, step
    for phase in PHASES:
        for index, step in enumerate(case.get(phase) or []):
            yield phase, index, step


def iter_assertions(case: dict[str, Any]):
    for index, assertion in enumerate(case.get("assertions") or []):
        yield index, assertion


def all_step_ids(case: dict[str, Any]) -> set[str]:
    return {str(step.get("id")) for _, _, step in iter_steps(case) if step.get("id")}


def _check_schema_version(case: dict[str, Any]) -> list[ValidationIssue]:
    version = case.get("schema_version")
    if version in SUPPORTED_SCHEMA_VERSIONS:
        return []
    return [
        ValidationIssue(
            path="schema_version",
            message=(
                f"不支持的 schema_version: {version!r}，"
                f"当前支持 {list(SUPPORTED_SCHEMA_VERSIONS)}"
            ),
            kind="semantics",
        )
    ]


def _check_unique_ids(case: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    seen: dict[str, str] = {}
    for phase, index, step in iter_steps(case):
        step_id = step.get("id")
        if not step_id:
            continue
        if step_id in seen:
            issues.append(
                ValidationIssue(
                    path=f"{phase}[{index}].id",
                    message=f"步骤 id 重复: {step_id!r}（已在 {seen[step_id]} 使用）",
                    kind="semantics",
                )
            )
        else:
            seen[step_id] = f"{phase}[{index}]"

    # 断言 id：顶层必填且唯一；all/any 子项自动编号
    assertion_ids: dict[str, str] = {}
    for index, assertion in enumerate(case.get("assertions") or []):
        aid = assertion.get("id")
        if not aid:
            issues.append(
                ValidationIssue(
                    path=f"assertions[{index}].id",
                    message="顶层断言必须提供 id",
                    kind="semantics",
                )
            )
            continue
        if aid in assertion_ids:
            issues.append(
                ValidationIssue(
                    path=f"assertions[{index}].id",
                    message=f"断言 id 重复: {aid!r}（已在 {assertion_ids[aid]} 使用）",
                    kind="semantics",
                )
            )
        else:
            assertion_ids[aid] = f"assertions[{index}]"

    return issues


def _check_variable_references(case: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    step_ids = all_step_ids(case)
    variables = set((case.get("variables") or {}))
    secrets = set((case.get("secrets") or {}))
    env_keys = set((case.get("environment") or {}))

    def walk(node: Any, path: str, *, in_secrets_block: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                walk(value, child, in_secrets_block=(child == "secrets"))
            return
        if isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]", in_secrets_block=in_secrets_block)
            return
        if not isinstance(node, str):
            return
        # secrets 块的值是环境变量名，不是引用
        if in_secrets_block:
            return

        for expr in iter_references(node):
            if not PATH_RE.match(expr):
                issues.append(
                    ValidationIssue(
                        path=path,
                        message=f"变量引用格式不合法: {{{{ {expr} }}}}（应为 {{ 命名空间.路径 }}）",
                        kind="semantics",
                    )
                )
                continue

            root, _, rest = expr.partition(".")
            if root not in NAMESPACES:
                issues.append(
                    ValidationIssue(
                        path=path,
                        message=(
                            f"未知命名空间 {{{{ {expr} }}}}，"
                            f"可用: {', '.join(NAMESPACES)}"
                        ),
                        kind="semantics",
                    )
                )
                continue

            if root in STATIC_KEYS:
                if rest not in STATIC_KEYS[root]:
                    issues.append(
                        ValidationIssue(
                            path=path,
                            message=(
                                f"{{{{ {expr} }}}} 不合法，"
                                f"{root} 可用键: {', '.join(sorted(STATIC_KEYS[root]))}"
                            ),
                            kind="semantics",
                        )
                    )
            elif root == "steps":
                step_id = rest.split(".")[0]
                if step_id and step_id not in step_ids:
                    issues.append(
                        ValidationIssue(
                            path=path,
                            message=(
                                f"{{{{ {expr} }}}} 引用了不存在的步骤 {step_id!r}，"
                                f"已声明: {', '.join(sorted(step_ids)) or '(无)'}"
                            ),
                            kind="semantics",
                        )
                    )
            elif root == "vars":
                name = rest.split(".")[0]
                if name and name not in variables and name not in secrets:
                    issues.append(
                        ValidationIssue(
                            path=path,
                            message=(
                                f"{{{{ {expr} }}}} 引用了未定义变量 {name!r}，"
                                f"已声明: {', '.join(sorted(variables | secrets)) or '(无)'}"
                            ),
                            kind="semantics",
                        )
                    )
            elif root == "secrets":
                name = rest.split(".")[0]
                if name and name not in secrets:
                    issues.append(
                        ValidationIssue(
                            path=path,
                            message=(
                                f"{{{{ {expr} }}}} 引用了未声明的 secret {name!r}，"
                                f"已声明: {', '.join(sorted(secrets)) or '(无)'}"
                            ),
                            kind="semantics",
                        )
                    )
            elif root == "env":
                name = rest.split(".")[0]
                if name and name not in env_keys:
                    issues.append(
                        ValidationIssue(
                            path=path,
                            message=(
                                f"{{{{ {expr} }}}} 引用了未定义的 environment 键 {name!r}，"
                                f"已声明: {', '.join(sorted(env_keys)) or '(无)'}"
                            ),
                            kind="semantics",
                        )
                    )

    walk(case, "")
    return issues


_SENSITIVE = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "cookie",
    "authorization",
    "credential",
)


def _check_plaintext_secrets(case: dict[str, Any]) -> list[ValidationIssue]:
    """敏感字段的**字面量**值一律拒绝，只允许 `{{ secrets.xxx }}`。"""
    issues: list[ValidationIssue] = []

    def walk(node: Any, path: str, *, in_secrets_block: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if child == "secrets":
                    in_secrets_block = True
                low = str(key).lower().replace("-", "_")
                if (
                    not in_secrets_block
                    and isinstance(value, str)
                    and value.strip()
                    and any(s in low for s in _SENSITIVE)
                ):
                    if not TEMPLATE_RE.search(value):
                        issues.append(
                            ValidationIssue(
                                path=child,
                                message=(
                                    "禁止明文凭据；请用 {{ secrets.<逻辑名> }} 引用，"
                                    "真实值通过环境变量注入"
                                ),
                                kind="security",
                            )
                        )
                walk(value, child, in_secrets_block=in_secrets_block)
            return
        if isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]", in_secrets_block=in_secrets_block)

    walk(case, "")
    return issues


def validate_file(path: str | Path) -> ValidationResult:
    case = load_case(path)
    return validate_case(case, source=str(path))
