"""v3 task/plan: letter ids, checkboxes, auto-promote, plan state machine,
task visibility in rendered views."""
import json

import pytest

from proj import plan as planmod
from proj import records as rec
from proj import task as taskmod
from proj.views import _norm_id


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


def test_plan_field_stored(proj):
    planmod.new(proj, "P1")
    row, _ = rec.new_record(proj, "task", "Attached", fields={"plan": "1"})
    assert row["plan"] == "1"
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"], by="id")["plan"] == "1"
    db.close()


def test_sync_auto_promotes_on_first_tick(proj):
    row, _ = rec.new_record(proj, "task", "T1")
    _write_task_steps(proj, row["id"], _steps_text().replace("[ ] step one", "[x] step one"))
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any("auto status" in n for n in notes)
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"], by="id")["status"] == "In progress"
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
    assert "Started: 20" in text
    assert "Done: 20" in text


def test_visibility_draft_plan_parks_tasks(proj):
    planmod.new(proj, "P1")  # Draft
    rec.new_record(proj, "task", "Attached", fields={"plan": "1"})
    rec.new_record(proj, "task", "Detached")
    tl = (proj.root / "TASKLIST.md").read_text()
    assert "Detached" in tl
    assert "Attached" not in tl  # parked in the Draft plan


def test_visibility_active_plan_promotes(proj):
    planmod.new(proj, "P1")
    rec.new_record(proj, "task", "Attached", fields={"plan": "1"})
    planmod.transition(proj, "activate", "1")
    tl = (proj.root / "TASKLIST.md").read_text()
    assert "Attached" in tl


def test_plan_task_list_view_section(proj):
    planmod.new(proj, "P1")
    row, _ = rec.new_record(proj, "task", "Attached", fields={"plan": "1"})
    _write_task_steps(proj, row["id"], _steps_text())
    # add an open item
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    r = db.get(spec, row["id"], by="id")
    sections = json.loads(r["sections"])
    sections["open"] = "need GPU budget decision"
    db.set(spec, row["uuid"], sections=json.dumps(sections))
    rec.render_all(proj)
    db.close()
    plan_md = next((proj.root / "docs/plans").glob("0001-*.md")).read_text()
    assert "Attached" in plan_md
    assert "need GPU budget decision" in plan_md


def test_review_requires_all_done(proj):
    planmod.new(proj, "P1")
    rec.new_record(proj, "task", "T1", fields={"plan": "1"})
    planmod.transition(proj, "activate", "1")
    with pytest.raises(ValueError, match="unfinished"):
        planmod.transition(proj, "review", "1")


def test_state_machine_full_path(proj):
    planmod.new(proj, "P1")
    row, _ = rec.new_record(proj, "task", "T1", fields={"plan": "1"})
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


def test_norm_id():
    assert _norm_id("0001") == "1"
    assert _norm_id("A") == "A"
