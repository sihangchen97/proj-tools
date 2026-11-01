"""WIP.md render smoke (verification for the wip.py module — run it until the
WIP feature lands in tests/test_wip.py, then move it there)."""
from __future__ import annotations

from proj.engine import wip as wipmod
from proj.engine import store as rec
from proj.engine.store import load_registry, open_db


def _add_item(db, tid: str, no: str, text: str, state: str = "todo") -> None:
    """One checklist row straight into the DB (render-only smoke — the items
    engine is covered by its own tests)."""
    from proj.core.db import utc_now
    db.begin()
    db.conn.execute(
        "INSERT INTO items (record_id, no, pos, text, state, note, owner, due, "
        "\"commit\", trailer, created, closed_at, deleted) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, no, 0, text, state, "", "", "", "", "", utc_now(), "", 0))
    db.conn.commit()


def test_wip_empty_placeholder(proj):
    db = open_db(proj)
    v = wipmod.render_wip_md(proj, db, load_registry(proj.root))
    db.close()
    assert v.path == proj.root / "WIP.md"
    assert "_nothing in progress_" in v.text
    assert "proj wip add" in v.text  # footer explains the commands


def test_wip_entry_shape(proj):
    db = open_db(proj)
    row, _ = rec.new_record(proj, "task", "Add login form", summary="Users sign in.")
    rec.status_set(proj, "task", row["id"], "In Progress")
    db.begin()
    db.wip_add(row["id"], "auto")
    db.conn.commit()
    _add_item(db, row["id"], "001", "form renders")
    _add_item(db, row["id"], "002", "docs updated", state="done")
    v = wipmod.render_wip_md(proj, db, load_registry(proj.root))
    db.close()
    t = v.text
    # §5.4: ## ID. title + entry marker + metadata line (emoji, status, type,
    # link, n/m) + italic summary + item list
    assert "## T-000001. Add login form" in t
    assert "<!-- proj:wip-entry T-000001 -->" in t
    assert "> 🚧 In Progress · task · [T-000001-add-login-form](docs/tasks/" \
           "T-000001-add-login-form.md) · 1/2" in t
    assert "_Users sign in._" in t
    assert "- [ ] #001 form renders" in t
    assert "- [x] #002 docs updated" in t


def test_wip_task_subtask_progress(proj):
    """A task WITHOUT checklist items shows its terminal/total sub-tasks."""
    db = open_db(proj)
    row, _ = rec.new_record(proj, "task", "Parent")
    sub, _ = rec.new_record(proj, "task", "Child")
    db.begin()
    db.set_parent(sub["id"], row["id"])
    db.conn.commit()
    rec.status_set(proj, "task", row["id"], "In Progress")
    rec.status_set(proj, "task", sub["id"], "Done")
    db.begin()
    db.wip_add(row["id"], "manual")
    db.conn.commit()
    v = wipmod.render_wip_md(proj, db, load_registry(proj.root))
    db.close()
    assert "· 1/1" in v.text  # 1 terminal of 1 sub-task


def test_wip_multiple_entries_list_order(proj):
    db = open_db(proj)
    a, _ = rec.new_record(proj, "task", "First")
    b, _ = rec.new_record(proj, "task", "Second")
    db.begin()
    db.wip_add(b["id"], "manual")
    db.wip_add(a["id"], "manual")  # appended AFTER b (list order = pos)
    db.conn.commit()
    v = wipmod.render_wip_md(proj, db, load_registry(proj.root))
    db.close()
    assert v.text.index("## T-000002.") < v.text.index("## T-000001.")
