"""F6: incremental render (`store.render_affected`).

Every write command now ends in `render_affected` (the affected set per F6.1,
"better too many than too few") instead of a full `render_all`. These tests
drive the PRODUCTION commands and assert: (a) the neighbours the F6.1 table
promises were updated on disk, (b) an unrelated record was NOT re-rendered
(mtime unchanged — the point of F6), (c) the result is in sync with a full
`render_all` (which then writes nothing).
"""
from proj.engine import item_ops as ops
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.record_types import task as taskmod


def _md(proj, rt, id_):
    reg = rec.load_registry(proj.root)
    with rec.open_db(proj) as db:
        row = db.get(reg[rt], id_)
    p = rec.file_path(proj, reg[rt], row)
    return p, p.read_text(encoding="utf-8")


def _mtime(proj, rt, id_):
    reg = rec.load_registry(proj.root)
    with rec.open_db(proj) as db:
        row = db.get(reg[rt], id_)
    return rec.file_path(proj, reg[rt], row).stat().st_mtime_ns


def test_done_renders_ancestor_chain(proj):
    """F6.1: a task change re-renders itself + every ancestor task (the Tasks
    block shows the subtree) and leaves unrelated records' files untouched."""
    top, _ = taskmod.new(proj, "Top")
    mid, _ = taskmod.new(proj, "Mid", parent=top["id"])
    leaf, _ = taskmod.new(proj, "Leaf", parent=mid["id"])
    other, _ = rec.new_record(proj, "decision", "Unrelated decision")
    other_mt = _mtime(proj, "decision", other["id"])

    taskmod.start(proj, leaf["id"])
    taskmod.done(proj, leaf["id"])  # production path -> render_affected

    # the leaf shows up in mid's and top's Tasks blocks
    _p, mid_text = _md(proj, "task", mid["id"])
    _p, top_text = _md(proj, "task", top["id"])
    assert leaf["id"] in mid_text  # the subtree renders in the parent
    assert leaf["id"] in top_text  # and in the grandparent
    # the unrelated record's file was not rewritten
    assert _mtime(proj, "decision", other["id"]) == other_mt
    # in sync with a full render (nothing left to write)
    assert rec.render_all(proj) == []


def test_done_renders_wip_when_member(proj):
    """F6.1: a WIP member reaching a terminal status re-renders WIP.md (the
    member auto-returns out of the list)."""
    leaf, _ = rec.new_record(proj, "task", "Leaf")
    rec.status_set(proj, "task", leaf["id"], "In Progress")  # wip.enter -> WIP.md
    taskmod.done(proj, leaf["id"])  # a WIP member reached a terminal state
    wip = (proj.root / "WIP.md").read_text(encoding="utf-8")
    assert leaf["id"] not in wip  # auto-returned from WIP.md
    assert rec.render_all(proj) == []  # WIP.md stayed in sync


def test_title_rename_reaches_every_neighbour(proj):
    """F6.3: renaming a record re-renders its parent / children / related and
    the records that pick it; the old file is deleted and every neighbour's md
    carries the NEW title (its link points at the new file)."""
    parent, _ = taskmod.new(proj, "Parent task")
    taskmod.new(proj, "Child task", parent=parent["id"])
    plan, _ = rec.new_record(proj, "plan", "Related plan")
    decision, _ = rec.new_record(proj, "decision", "A decision")
    record_for("plan", rec.load_registry(proj.root)).add_related(
        proj, plan["id"], [decision["id"]])
    # a checklist item that picks the parent task (its `picked by` shows the link)
    ops.add(proj, decision["id"], f"criterion {parent['id']}")

    old_p, _ = _md(proj, "plan", plan["id"])
    rec.update_record(proj, "plan", plan["id"], title="Related plan FINAL")

    new_p, _ = _md(proj, "plan", plan["id"])
    assert new_p != old_p and new_p.exists() and not old_p.exists()
    # the related neighbour's `related:` link re-points at the renamed file
    # (the anchor is the slug, so propagation shows as the new slug)
    _p, text = _md(proj, "decision", decision["id"])
    assert "related-plan-final" in text, f"{decision['id']} still links the old file"
    assert "related-plan" in text and "related-plan.md" not in text  # not the old slug
    assert rec.render_all(proj) == []  # incremental already reached everything


def test_incremental_equals_full_after_item_check(proj):
    """F6.1: checking a criterion re-renders the record + its ancestor tasks —
    byte-identical to a full render (which then writes nothing)."""
    top, _ = taskmod.new(proj, "Top")
    mid, _ = taskmod.new(proj, "Mid", parent=top["id"])
    leaf, _ = taskmod.new(proj, "Leaf", parent=mid["id"])
    ops.add(proj, leaf["id"], "criterion x")
    taskmod.start(proj, leaf["id"])
    ops.check(proj, f"{leaf['id']}#001")
    for rid in (leaf["id"], mid["id"], top["id"]):
        _p, text = _md(proj, "task", rid)
        assert leaf["id"] in text  # the subtree renders in every ancestor
    assert rec.render_all(proj) == []
    _p, leaf_text = _md(proj, "task", leaf["id"])
    assert "criterion x" in leaf_text


def test_unrelated_record_file_not_rewritten_on_new(proj):
    """F6: creating one record re-renders only that record (+ WIP.md on
    enter) — an existing unrelated record's file keeps its mtime."""
    existing, _ = rec.new_record(proj, "decision", "Existing decision")
    mt = _mtime(proj, "decision", existing["id"])
    rec.new_record(proj, "plan", "A new plan")
    assert _mtime(proj, "decision", existing["id"]) == mt
    assert rec.render_all(proj) == []
