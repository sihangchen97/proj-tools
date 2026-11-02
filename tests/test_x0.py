"""X0: the unified write entry point + change tracking (plans/hard-parts X0).

- every write through Db marks `touched` / `touched_views` (F6 renders from
  these; C2 feeds events from the `_emit` hooks — a no-op in X0);
- no raw INSERT/UPDATE/DELETE outside core/db.py (replay.py is the documented
  exception: a full replay rebuilds the whole DB from the event log, not one
  command's write path).
"""
from __future__ import annotations

import re
from pathlib import Path

from proj.engine import store as rec

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
#: allowed to write raw (documented): the full-replay DB rebuild
RAW_SQL_WHITELIST = {"src/proj/replay.py"}
WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE)\b", re.I)


def test_no_raw_write_sql_outside_db():
    """X0.2.1: scan src/ — no conn.execute carrying a write statement outside
    core/db.py (read-only SELECT stays allowed). Replay is whitelisted; route
    it through Db methods if C4 ever needs to."""
    problems = []
    for py in sorted(SRC.rglob("*.py")):
        rel = py.relative_to(REPO).as_posix()
        if "core/db.py" in rel or rel in RAW_SQL_WHITELIST:
            continue
        for i, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), 1):
            if "execute" in line and WRITE.search(line):
                problems.append(f"{rel}:{i}: {line.strip()}")
    assert not problems, ("raw write SQL found (use a Db method):\n"
                          + "\n".join(problems))


def _open_write_db(proj):
    """An open write-transaction Db for asserting `touched` directly."""
    db = rec.open_db(proj)
    db.begin()
    return db


def test_touched_tracks_write_methods(proj):
    """X0.2.2: every Db write method marks the record(s) it wrote (+ views)."""
    db = _open_write_db(proj)
    spec = rec.load_registry(proj.root)["decision"]
    try:
        db.insert(spec, "DEC-0090", "T", "Proposed", "2026-01-01")
        assert db.touched == {"DEC-0090"}
        db.set(spec, "DEC-0090", status="Accepted")
        db.add_log("DEC-0090", "changed", category="Changed")
        db.add_relation("DEC-0090", "DEC-0001", kind="supersede")
        assert db.touched == {"DEC-0090", "DEC-0001"}
        db.items_save("DEC-0090",
                      [dict(no="001", text="x", section="Work")])
        db.wip_add("DEC-0090", "test")
        assert db.touched_views == {"wip"}
        db.add_cl_entry("Unreleased", "Added", "x")
        db.release_cl("v9.9.9", "Unreleased")
        assert "changelog" in db.touched_views
        db.wip_remove("DEC-0090")
        db.remove_relation("DEC-0090", "DEC-0001", kind="supersede")
        db.item_set_state("DEC-0090", "001", "done", "2026-01-02")
        db.delete(spec, "DEC-0090")
        assert "DEC-0090" in db.touched
        db.conn.rollback()
    finally:
        db.conn.close()


def test_set_parent_touches_old_and_new_parent(proj):
    """X0.2.2: set_parent marks the child, the OLD parent and the new one."""
    import proj.record_types.task as taskmod

    parent_a, _ = taskmod.new(proj, "PA")
    parent_b, _ = taskmod.new(proj, "PB")
    child, _ = taskmod.new(proj, "C", parent=parent_a["id"])
    db = _open_write_db(proj)
    try:
        db.set_parent(child["id"], parent_b["id"])
        assert db.touched == {parent_a["id"], parent_b["id"], child["id"]}
        db.conn.rollback()
    finally:
        db.conn.close()


def test_touched_cleared_on_rollback(proj):
    """A failed command rolls back and its change tracking is cleared too —
    and the rolled-back row really is gone."""
    db = _open_write_db(proj)
    spec = rec.load_registry(proj.root)["decision"]
    db.insert(spec, "DEC-0091", "T", "Proposed", "2026-01-01")
    assert db.touched == {"DEC-0091"}
    db.rollback()
    assert db.touched == set()
    db.conn.close()
    with rec.open_db(proj) as check:
        assert check.get(spec, "DEC-0091") is None
