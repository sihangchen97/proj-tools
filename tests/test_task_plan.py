"""v3 task/plan: digit ids, checkboxes, auto-promote, plan state machine,
task visibility in rendered views."""
import pytest

from proj import check as checkmod
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.record_types import task as taskmod


# plan has no Record subclass: its state machine comes straight from the template yaml
def _plan(proj):
    return record_for("plan", rec.load_registry(proj.root))


def _steps_text():
    return "## Steps\n\n- [ ] step one\n  - [ ] sub one\n- [ ] step two\n"


def _write_task_steps(proj, task_id, steps: str) -> None:
    p = proj.root / "docs" / "tasks" / next((proj.root / "docs" / "tasks").glob(f"{task_id}-*.md"))
    t = p.read_text()
    head, _, _ = t.partition("## Steps")
    p.write_text(head + steps)


def test_digit_ids_increment(proj):
    r1, _ = rec.new_record(proj, "task", "T1")
    r2, _ = rec.new_record(proj, "task", "T2")
    r3, _ = rec.new_record(proj, "task", "T3")
    assert (r1["id"], r2["id"], r3["id"]) == ("T-000001", "T-000002", "T-000003")


def test_parent_relation_stored(proj):
    _plan(proj).new(proj, "P1")
    row, _ = taskmod.new(proj, "Attached", parent="PLN-0001")
    # parent is a relations edge (kind=parent, source=parent), not a column
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert "parent" not in db.get(spec, row["id"])
    assert db.parent_of(row["id"])["source"] == "PLN-0001"
    assert [c["target"] for c in db.children_of("PLN-0001")] == [row["id"]]
    db.close()


def test_summary_roundtrip(proj):
    # one-sentence summary: stored in the one_sentence column, rendered as an
    # italic line in the head fence (no "summary:" label), md wins
    row, p = rec.new_record(proj, "task", "S1", summary="kickoff")
    assert row["one_sentence"] == "kickoff"
    text = p.read_text()
    assert "_kickoff_" in text.splitlines()  # italic line, not a labeled row
    p.write_text(text.replace("_kickoff_", "_shipped the core_"))
    rec.sync(proj)
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"])["one_sentence"] == "shipped the core"
    db.close()


def test_sync_auto_promotes_on_first_tick(proj):
    row, _ = rec.new_record(proj, "task", "T1")
    _write_task_steps(proj, row["id"], _steps_text().replace("[ ] step one", "[x] step one"))
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any("auto status" in n for n in notes)
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"])["status"] == "In Progress"
    db.close()


def test_done_requires_all_checked(proj):
    row, _ = rec.new_record(proj, "task", "T1")
    _write_task_steps(proj, row["id"], _steps_text())
    rec.sync(proj)
    with pytest.raises(ValueError, match="unchecked"):
        taskmod.done(proj, row["id"])
    _write_task_steps(proj, row["id"],
                      _steps_text().replace("[ ]", "[x]"))
    rec.sync(proj)
    row2 = taskmod.done(proj, row["id"])
    assert row2["status"] == "Done"


def test_logs_limit_env(proj, monkeypatch):
    """PROJ_LOGS: 0 = no changelog block, -1 = all, default = 5, N = latest N.
    The record has 9 changelog rows (created + events 0..7), each rendered as a
    backticked entry `- <date> - <body>` (comment → the comment text)."""
    row, _ = rec.new_record(proj, "task", "T1")
    for i in range(8):
        rec.add_log(proj, "task", row["id"], f"event {i}")
    p = next((proj.root / "docs/tasks").glob(f"{row['id']}-*.md"))
    monkeypatch.setenv("PROJ_LOGS", "0")
    rec.render_all(proj)
    assert "<details>" not in p.read_text()
    monkeypatch.setenv("PROJ_LOGS", "-1")
    rec.render_all(proj)
    assert all(f"event {i}`" in p.read_text() for i in range(8))
    monkeypatch.setenv("PROJ_LOGS", "2")
    rec.render_all(proj)
    text = p.read_text()
    assert "event 7`" in text and "event 6`" in text
    assert "event 5`" not in text
    monkeypatch.setenv("PROJ_LOGS", "")  # empty → default 5: events 7,6,5,4,3
    rec.render_all(proj)
    text = p.read_text()
    assert "event 7`" in text and "event 3`" in text
    assert "event 2`" not in text
    monkeypatch.delenv("PROJ_LOGS")  # unset → default 5
    rec.render_all(proj)
    assert "event 2`" not in p.read_text()


def test_done_sets_derived_dates(proj):
    row, _ = rec.new_record(proj, "task", "T1")
    _write_task_steps(proj, row["id"], _steps_text().replace("[ ]", "[x]"))
    rec.sync(proj)
    taskmod.done(proj, row["id"])
    p = next((proj.root / "docs" / "tasks").glob(f"{row['id']}-*.md"))
    text = p.read_text()
    # created/done merged into one row; done → the created→done dates
    assert "> **created/done:** 20" in text
    assert " → 20" in text


def test_attached_tasks_show_in_main_tasklist(proj):
    """Main TASKLIST shows the whole tree: plans as top-level nodes, their
    tasks (kind=parent relation) indented below, top-level tasks alongside."""
    _plan(proj).new(proj, "P1")  # Draft
    attached, _ = taskmod.new(proj, "Attached", parent="PLN-0001")
    detached, _ = rec.new_record(proj, "task", "Detached")
    tl = (proj.root / "TASKLIST.md").read_text()
    plan_md = next((proj.root / "docs/plans").glob("PLN-0001-*.md")).name
    plan_id = plan_md.split("-")[0]
    lines = tl.splitlines()
    assert "plan" in tl and f"{detached['id']}-" in tl  # task lines are hyperlinks
    assert f"{attached['id']}-" in tl  # hangs under the plan node, indented
    plan_line = next(i for i, line in enumerate(lines) if f"plan {plan_id}" in line)
    assert lines[plan_line + 1].startswith("  - [")  # checkbox + hyperlink
    assert f"{attached['id']}-" in lines[plan_line + 1]


def test_plan_task_tree_rendered_from_db(proj):
    """The plan's task tree is rendered from kind=parent relations — a checkbox
    + hyperlink per task (no body section, no ## heading), below the head
    fence."""
    _plan(proj).new(proj, "P1")
    row, _ = taskmod.new(proj, "Attached", parent="PLN-0001")
    sub, _ = taskmod.new(proj, "Substep", parent=row["id"])
    plan_md = next((proj.root / "docs/plans").glob("PLN-0001-*.md")).read_text()
    assert "> **subtask:**" in plan_md
    assert f"[{row['id']}-" in plan_md  # task lines are filename hyperlinks
    assert f"[{sub['id']}-" in plan_md
    assert "## Task List" not in plan_md
    # same tree in the main TASKLIST
    tl = (proj.root / "TASKLIST.md").read_text()
    assert f"{row['id']}-" in tl and f"{sub['id']}-" in tl


def test_experiment_contains_task_makes_it_a_tree_root(proj):
    """Any contain_task record_type renders as a TASKLIST root (experiment, like
    plan) — its tasks hang indented below, no code needed."""
    rec.new_record(proj, "experiment", "Exp A")
    row, _ = taskmod.new(proj, "Run baseline", parent="EXP-0001")
    sub, _ = taskmod.new(proj, "Plot results", parent=row["id"])
    exp_md = next((proj.root / "docs/experiments").glob("EXP-0001-*.md")).read_text()
    assert f"[{row['id']}-" in exp_md and f"[{sub['id']}-" in exp_md
    tl = (proj.root / "TASKLIST.md").read_text()
    lines = tl.splitlines()
    exp_line = next(i for i, line in enumerate(lines) if "experiment" in line)
    assert lines[exp_line + 1].startswith("  - [") and f"{row['id']}-" in lines[exp_line + 1]


def test_state_machine_full_path(proj):
    _plan(proj).new(proj, "P1")
    row, _ = taskmod.new(proj, "T1", parent="PLN-0001")
    _write_task_steps(proj, row["id"], _steps_text().replace("[ ]", "[x]"))
    rec.sync(proj)
    taskmod.done(proj, row["id"])
    _plan(proj).transition(proj, "activate", "PLN-0001")
    _plan(proj).transition(proj, "review", "PLN-0001")
    _plan(proj).transition(proj, "shelve", "PLN-0001")
    _plan(proj).transition(proj, "resume", "PLN-0001")
    _plan(proj).transition(proj, "review", "PLN-0001")
    _plan(proj).transition(proj, "archive", "PLN-0001")
    spec = rec.load_registry(proj.root)["plan"]
    db = rec.open_db(proj)
    assert db.get(spec, "PLN-0001")["status"] == "Archived"
    db.close()


def test_review_blocks_on_undone_direct_child(proj):
    """plan review gate: every direct child task must be Done (sub-tasks under a
    task do not block — only direct children do)."""
    _plan(proj).new(proj, "P1")
    _plan(proj).transition(proj, "activate", "PLN-0001")
    row, _ = taskmod.new(proj, "Open task", parent="PLN-0001")
    _write_task_steps(proj, row["id"], _steps_text())
    rec.sync(proj)
    with pytest.raises(ValueError, match="not Done"):
        _plan(proj).transition(proj, "review", "PLN-0001")
    # a sub-task under the task (not a direct child of the plan) never blocks
    taskmod.new(proj, "Substep", parent=row["id"])
    with pytest.raises(ValueError, match="not Done"):
        _plan(proj).transition(proj, "review", "PLN-0001")
    _write_task_steps(proj, row["id"], _steps_text().replace("[ ]", "[x]"))
    rec.sync(proj)
    taskmod.done(proj, row["id"])
    row2 = _plan(proj).transition(proj, "review", "PLN-0001")
    assert row2["status"] == "Pending Review"


def test_illegal_transition_rejected(proj):
    _plan(proj).new(proj, "P1")  # Draft
    with pytest.raises(ValueError, match="requires"):
        _plan(proj).transition(proj, "shelve", "PLN-0001")


def test_shelved_freeze_to_archived(proj):
    _plan(proj).new(proj, "P1")
    _plan(proj).transition(proj, "activate", "PLN-0001")
    _plan(proj).transition(proj, "review", "PLN-0001")
    _plan(proj).transition(proj, "shelve", "PLN-0001")
    _plan(proj).transition(proj, "freeze", "PLN-0001")
    spec = rec.load_registry(proj.root)["plan"]
    db = rec.open_db(proj)
    assert db.get(spec, "PLN-0001")["status"] == "Archived"
    db.close()
    with pytest.raises(ValueError, match="requires"):
        _plan(proj).transition(proj, "freeze", "PLN-0001")  # Archived: no freeze


def test_task_rejects_ghost_parent_ref(proj):
    with pytest.raises(ValueError, match=r"not found in any record_type"):
        taskmod.new(proj, "T1", parent="999")
    # valid ref (any type) still works, and a bare task (no parent) is fine
    _plan(proj).new(proj, "P1")
    row, _ = taskmod.new(proj, "T1", parent="PLN-0001")
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.parent_of(row["id"])["source"] == "PLN-0001"
    db.close()
    taskmod.new(proj, "T2")


def test_fk_rejects_dangling_parent(proj):
    """D8: a parent edge to a nonexistent record is refused by the FK (the
    write-side guard rejects it before ever reaching the DB)."""
    import sqlite3
    _plan(proj).new(proj, "P1")
    row, _ = rec.new_record(proj, "task", "T1")
    db = rec.open_db(proj)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_relation("PLN-0099", row["id"], "parent")
    db.close()
    # a valid parent edge passes
    db = rec.open_db(proj)
    db.add_relation("PLN-0001", row["id"], "parent")
    db.close()
    assert checkmod.check(proj) == []


def test_done_not_blocked_by_subtasks(proj):
    # sub-tasks are independent: a task is done by its own steps alone,
    # whether or not its sub-tasks are done (parent never waits for children)
    parent_row, _ = rec.new_record(proj, "task", "T1")
    sub, _ = taskmod.new(proj, "Substep", parent=parent_row["id"])
    _write_task_steps(proj, parent_row["id"], _steps_text().replace("[ ]", "[x]"))
    rec.sync(proj)
    taskmod.done(proj, parent_row["id"])
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, parent_row["id"])["status"] == "Done"
    assert db.get(spec, sub["id"])["status"] != "Done"
    db.close()


def test_children_rendered_indented_in_main_tasklist(proj):
    _plan(proj).new(proj, "P1")
    parent_row, _ = taskmod.new(proj, "T1", parent="PLN-0001")
    sub_row, _ = taskmod.new(proj, "Substep", parent=parent_row["id"])
    rec.render_all(proj)
    tl = (proj.root / "TASKLIST.md").read_text()
    # sub-task indented under its parent task, which sits under the plan node
    plan_md = next((proj.root / "docs/plans").glob("PLN-0001-*.md")).name
    plan_id = plan_md.split("-")[0]
    lines = tl.splitlines()
    plan_line = next(i for i, line in enumerate(lines) if f"plan {plan_id}" in line)
    parent_line = next(i for i, line in enumerate(lines) if parent_row["id"] in line)
    sub_line = next(i for i, line in enumerate(lines) if sub_row["id"] in line)
    assert parent_line == plan_line + 1 and sub_line == parent_line + 1
    assert lines[parent_line].startswith("  - ") and lines[sub_line].startswith("    - ")
