"""v3 task/plan: letter ids, checkboxes, auto-promote, plan state machine,
task visibility in rendered views."""
import pytest

from proj import check as checkmod
from proj.engine import store as rec
from proj.engine.views import _norm_id
from proj.record_types import plan as planmod
from proj.record_types import task as taskmod


def _steps_text():
    return "## Steps\n\n- [ ] step one\n  - [ ] sub one\n- [ ] step two\n"


def _write_task_steps(proj, task_id, steps: str) -> None:
    p = proj.root / "tasks" / next((proj.root / "tasks").glob(f"{task_id}-*.md"))
    t = p.read_text()
    head, _, _ = t.partition("## Steps")
    p.write_text(head + steps)


def test_letter_ids_increment(proj):
    r1, _ = rec.new_record(proj, "task", "T1")
    r2, _ = rec.new_record(proj, "task", "T2")
    r3, _ = rec.new_record(proj, "task", "T3")
    assert (r1["id"], r2["id"], r3["id"]) == ("A", "B", "C")


def test_parent_field_stored(proj):
    planmod.new(proj, "P1")
    row, _ = rec.new_record(proj, "task", "Attached", fields={"parent": "1"})
    assert row["parent"] == "1"
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"], by="id")["parent"] == "1"
    db.close()


def test_summary_roundtrip(proj):
    # one-sentence summary: managed header line, hand-editable in md (md wins)
    row, p = rec.new_record(proj, "task", "S1", summary="kickoff")
    assert row["summary"] == "kickoff"
    text = p.read_text()
    assert "Summary: kickoff" in text
    p.write_text(text.replace("Summary: kickoff", "Summary: shipped the core"))
    rec.sync(proj)
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"], by="id")["summary"] == "shipped the core"
    db.close()


def test_sync_auto_promotes_on_first_tick(proj):
    row, _ = rec.new_record(proj, "task", "T1")
    _write_task_steps(proj, row["id"], _steps_text().replace("[ ] step one", "[x] step one"))
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any("auto status" in n for n in notes)
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"], by="id")["status"] == "In Progress"
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


def test_done_sets_derived_dates(proj):
    row, _ = rec.new_record(proj, "task", "T1")
    _write_task_steps(proj, row["id"], _steps_text().replace("[ ]", "[x]"))
    rec.sync(proj)
    taskmod.done(proj, row["id"])
    p = next((proj.root / "tasks").glob(f"{row['id']}-*.md"))
    text = p.read_text()
    assert "started: 20" in text
    assert "done: 20" in text


def test_attached_tasks_show_in_main_tasklist(proj):
    """Main TASKLIST shows the whole tree: plans as top-level nodes, their
    tasks (DB parent link) indented below, top-level tasks alongside."""
    planmod.new(proj, "P1")  # Draft
    rec.new_record(proj, "task", "Attached", fields={"parent": "1"})
    rec.new_record(proj, "task", "Detached")
    tl = (proj.root / "TASKLIST.md").read_text()
    plan_md = next((proj.root / "docs/plans").glob("0001-*.md")).name
    plan_id = plan_md.split("-")[0]
    lines = tl.splitlines()
    assert "plan" in tl and "Detached" in tl
    assert "Attached" in tl  # hangs under the plan node, indented
    plan_line = next(i for i, line in enumerate(lines) if f"plan {plan_id}" in line)
    assert lines[plan_line + 1].startswith("  - ")
    assert "Attached" in lines[plan_line + 1]


def test_plan_task_list_section_rendered_from_db(proj):
    """The plan's Task List section is `type: task` template data — the engine
    renders the task subtree (parent == this plan, recursively) from the DB."""
    planmod.new(proj, "P1")
    row, _ = rec.new_record(proj, "task", "Attached", fields={"parent": "1"})
    rec.new_record(proj, "task", "Substep", fields={"parent": row["id"]})
    plan_md = next((proj.root / "docs/plans").glob("0001-*.md")).read_text()
    assert "## Task List" in plan_md
    assert "Attached" in plan_md
    assert "Substep" in plan_md
    # same tree in the main TASKLIST
    tl = (proj.root / "TASKLIST.md").read_text()
    assert "Attached" in tl and "Substep" in tl


def test_experiment_task_section_makes_it_a_tree_root(proj):
    """Any record_type with a `type: task` section renders as a TASKLIST root
    (experiment, like plan) — its tasks hang indented below, no code needed."""
    rec.new_record(proj, "experiment", "Exp A")
    row, _ = rec.new_record(proj, "task", "Run baseline", fields={"parent": "1"})
    rec.new_record(proj, "task", "Plot results", fields={"parent": row["id"]})
    exp_md = next((proj.root / "docs/experiments").glob("0001-*.md")).read_text()
    assert "## Task List" in exp_md and "Run baseline" in exp_md and "Plot results" in exp_md
    tl = (proj.root / "TASKLIST.md").read_text()
    lines = tl.splitlines()
    exp_line = next(i for i, line in enumerate(lines) if "experiment" in line)
    assert lines[exp_line + 1].startswith("  - ") and "Run baseline" in lines[exp_line + 1]


def test_state_machine_full_path(proj):
    planmod.new(proj, "P1")
    row, _ = rec.new_record(proj, "task", "T1", fields={"parent": "1"})
    _write_task_steps(proj, row["id"], _steps_text().replace("[ ]", "[x]"))
    rec.sync(proj)
    taskmod.done(proj, row["id"])
    planmod.transition(proj, "activate", "1")
    planmod.transition(proj, "review", "1")
    planmod.transition(proj, "shelve", "1")
    planmod.transition(proj, "resume", "1")
    planmod.transition(proj, "review", "1")
    planmod.transition(proj, "archive", "1")
    spec = rec.load_registry(proj.root)["plan"]
    db = rec.open_db(proj)
    assert db.get(spec, "0001", by="id")["status"] == "Archived"
    db.close()


def test_illegal_transition_rejected(proj):
    planmod.new(proj, "P1")  # Draft
    with pytest.raises(ValueError, match="requires"):
        planmod.transition(proj, "shelve", "1")


def test_shelved_freeze_to_archived(proj):
    planmod.new(proj, "P1")
    planmod.transition(proj, "activate", "1")
    planmod.transition(proj, "review", "1")
    planmod.transition(proj, "shelve", "1")
    planmod.transition(proj, "freeze", "1")
    spec = rec.load_registry(proj.root)["plan"]
    db = rec.open_db(proj)
    assert db.get(spec, "0001", by="id")["status"] == "Archived"
    db.close()
    with pytest.raises(ValueError, match="requires"):
        planmod.transition(proj, "freeze", "1")  # Archived: no freeze


def test_norm_id():
    assert _norm_id("0001") == "1"
    assert _norm_id("A") == "A"


def test_task_rejects_ghost_parent_ref(proj):
    with pytest.raises(ValueError, match=r"parent '999' not found in any record_type"):
        taskmod.new(proj, "T1", parent="999")
    # valid ref (any type) still works, and a bare task (no parent) is fine
    planmod.new(proj, "P1")
    row, _ = taskmod.new(proj, "T1", parent="1")
    assert row["parent"] == "1"
    taskmod.new(proj, "T2")


def test_check_flags_dangling_parent(proj):
    planmod.new(proj, "P1")
    rec.new_record(proj, "task", "T1", fields={"parent": "42"})  # bypass the CLI guard
    problems = checkmod.check(proj)
    assert any("parent 42 does not exist" in p for p in problems)
    # healthy refs pass once the dangling one is repointed
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    bad = db.get(spec, "A", by="id")
    db.set(spec, bad["uuid"], parent="1")
    db.close()
    assert not any("does not exist" in p for p in checkmod.check(proj))


def test_done_not_blocked_by_subtasks(proj):
    # sub-tasks are independent: a task is done by its own steps alone,
    # whether or not its sub-tasks are done (parent never waits for children)
    parent_row, _ = rec.new_record(proj, "task", "T1")
    sub, _ = rec.new_record(proj, "task", "Substep", fields={"parent": parent_row["id"]})
    _write_task_steps(proj, parent_row["id"], _steps_text().replace("[ ]", "[x]"))
    rec.sync(proj)
    taskmod.done(proj, parent_row["id"])
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, parent_row["id"], by="id")["status"] == "Done"
    assert db.get(spec, sub["id"], by="id")["status"] != "Done"
    db.close()


def test_children_rendered_indented_in_main_tasklist(proj):
    planmod.new(proj, "P1")
    parent_row, _ = rec.new_record(proj, "task", "T1", fields={"parent": "1"})
    rec.new_record(proj, "task", "Substep", fields={"parent": parent_row["id"]})
    rec.render_all(proj)
    tl = (proj.root / "TASKLIST.md").read_text()
    # sub-task indented under its parent task, which sits under the plan node
    plan_md = next((proj.root / "docs/plans").glob("0001-*.md")).name
    plan_id = plan_md.split("-")[0]
    lines = tl.splitlines()
    plan_line = next(i for i, line in enumerate(lines) if f"plan {plan_id}" in line)
    parent_line = next(i for i, line in enumerate(lines) if parent_row["id"] in line)
    sub_line = next(i for i, line in enumerate(lines) if "Substep" in line)
    assert parent_line == plan_line + 1 and sub_line == parent_line + 1
    assert lines[parent_line].startswith("  - ") and lines[sub_line].startswith("    - ")
