from __future__ import annotations

import pytest

from proj import scaffold
from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.engine import store as rec


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


@pytest.fixture
def many_tasks(proj) -> int:
    """T2 (§15.1): bulk-create 300 tasks in ONE db transaction.

    Do NOT build a large project by looping `rec.new_record` here — every
    call runs auto-sync + a full `render_all` (O(N^2)); the DB-level insert
    is the fast path, and `render_all` is what these tests exercise anyway."""
    spec = rec.load_registry(proj.root)["task"]
    db = dbmod.Db(proj.db_path)
    try:
        with db:  # one transaction for all 300 inserts
            for i in range(1, 301):
                db.insert(spec, f"T-{i:06d}", f"Task {i}",
                          spec.status_allowed[0], dbmod.utc_now())
        yield 300
    finally:
        db.close()
