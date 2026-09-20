"""配置加载（platform 侧）。

只负责**文件定位、读取、环境变量展开**；配置的**数据模型**在
`mtp_contracts.config`（`PlatformConfig` / `McpServerConfig`）。

"项目根"默认取当前工作目录（可用 `MTP_ROOT` 覆盖），所有相对路径按它解析 ——
这样安装成包之后也能正常工作，不依赖源码目录。

选择配置文件（优先级从高到低）：

1. `MTP_CONFIG` 环境变量；
2. `<root>/mtp_config.local.yaml`（本机专属，已 ignore）；
3. `<root>/mtp_config.yaml`（仓库里那份，只放可移植的写法）。
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

from mtp_contracts.config import PlatformConfig
from mtp_contracts.errors import ConfigError

# ${VAR} 与 ${VAR:-默认值}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

CONFIG_FILE = "mtp_config.yaml"
LOCAL_CONFIG_FILE = "mtp_config.local.yaml"


def project_root(root: str | os.PathLike[str] | None = None) -> Path:
    """相对路径的解析基准。默认当前工作目录，可用 MTP_ROOT / 显式参数覆盖。"""
    if root is not None:
        return Path(root).expanduser().resolve()
    env = os.environ.get("MTP_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def _default_config_path(root: Path) -> Path:
    from_env = os.environ.get("MTP_CONFIG")
    if from_env:
        return Path(from_env)
    local = root / LOCAL_CONFIG_FILE
    if local.exists():
        return local
    return root / CONFIG_FILE


def _expand_env(value: Any, missing: set[str]) -> Any:
    """递归展开字符串里的 `${VAR}` / `${VAR:-默认值}`。

    未设置且没有默认值的变量**不报错**，而是留空并记进 `missing`：
    加载阶段不提前阻断服务；任务创建或执行时会把实际配置问题返回页面/API。
    """
    if isinstance(value, dict):
        return {k: _expand_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, missing) for v in value]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        found = os.environ.get(name)
        if found is not None:
            return found
        if match.group(2) is not None:
            return match.group(2)
        missing.add(name)
        return ""

    return _ENV_REF.sub(repl, value)


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
) -> PlatformConfig:
    """读取配置；找不到文件时抛 ConfigError（附明确路径）。"""
    base = project_root(root)
    target = Path(path) if path else _default_config_path(base)
    if not target.is_absolute():
        target = (base / target).resolve()

    if not target.exists():
        raise ConfigError(
            f"配置文件不存在: {target}",
            detail="可用 MTP_CONFIG 环境变量指定其他路径，或 MTP_ROOT 改项目根",
        )

    with target.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点必须是映射: {target}")

    missing: set[str] = set()
    expanded = _expand_env(raw, missing)
    return PlatformConfig(
        raw=copy.deepcopy(expanded),
        path=target,
        missing_env=sorted(missing),
        base_dir=base,
    )


def resolve_secrets(mapping: dict[str, str] | None) -> dict[str, str]:
    """把 `{逻辑名: 环境变量名}` 解析成 `{逻辑名: 真实值}`。

    缺环境变量时抛 ConfigError —— 明确报错好过静默用空串跑出一个假失败。
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for logical, env_name in (mapping or {}).items():
        value = os.environ.get(str(env_name))
        if value is None:
            missing.append(str(env_name))
        else:
            resolved[str(logical)] = value
    if missing:
        raise ConfigError(
            f"缺少必需的环境变量: {', '.join(sorted(missing))}",
            detail="凭据只允许通过环境变量注入，禁止写入用例或配置文件",
        )
    return resolved
