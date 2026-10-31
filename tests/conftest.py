from __future__ import annotations

import pytest

from proj import scaffold
from proj.core.config import ProjConfig


@pytest.fixture
def root(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\n')
    return tmp_path


@pytest.fixture
def cfg(root):
    return ProjConfig(root=root)


@pytest.fixture
def proj(cfg):
    """A fully initialized project (DB tables, lock, seed decision)."""
    scaffold.init(cfg, ["default", "quality"])
    return cfg
