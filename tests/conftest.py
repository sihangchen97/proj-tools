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


render_verify = pytest.mark.render_verify


@pytest.fixture(autouse=True)
def _render_verify(tmp_path, request):
    """F6.2: after EVERY test the on-disk md must equal a full in-memory render
    of the DB — the incremental `render_affected` is cross-checked against
    `render_all` (incremental must == full). Default ON. Mark a test
    `@pytest.mark.render_drift` when it deliberately leaves the disk out of
    sync (the `proj check` drift tests assert the tool DETECTS it, so
    the disk is NOT supposed to match at teardown). e2e.py turns the same
    verification on for the whole story (default, every write command + a
    final sweep)."""
    yield
    if request.node.get_closest_marker("render_drift"):
        return  # this test intentionally leaves the disk drifted
    if not (tmp_path / ".proj" / "lock.yaml").exists():
        return  # no proj project in this test
    from proj.render_verify import verify

    diffs = verify(ProjConfig(root=tmp_path))
    if diffs:
        for rel, kind, detail in diffs[:5]:
            print(f"render-verify {kind}: {rel} ({detail})")
        raise AssertionError(f"disk not in sync with the DB render: {len(diffs)} file(s)")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "render_drift: the test leaves the disk drifted on purpose "
        "(skip the F6.2 render check)")
