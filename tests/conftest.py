"""pytest 公共夹具。"""

from __future__ import annotations

import pytest

from mtp_platform.config import load_config


@pytest.fixture(scope="session")
def config():
    """仓库版配置（项目根 = 当前工作目录）。"""
    return load_config()
