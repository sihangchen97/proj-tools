"""Phase-1 task tree: gates (descendant tasks, terminal statuses), task move,
derived TASKLIST (no T-000000, no registration), Tasks-block D11 checks."""
import pytest

from proj import check as checkmod
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.record_types import task as taskmod


def _plan(proj):
    return record_for("plan", rec.load_registry(proj.root))


def _task(proj):
    return record_for("task", rec.load_registry(proj.root))


def test_first_task_is_T_000001(proj):
    """No T-000000 seed anymore: the first task created is T-000001."""
    row, _ = rec.new_record(proj, "task", "First")
    assert row["id"] == "T-000001"
    db = rec.open_db(proj)
    assert db.get(rec.load_registry(proj.root)["task"], "T-000000") is None
    db.close()


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


def test_task_done_blocked_by_draft_child(proj):
    """gates.tasks: a parent task cannot go Done while a descendant (any depth)
    is not in a terminal task status; Done or Failed unblock it."""
    parent, _ = taskmod.new(proj, "Parent")
    child, _ = taskmod.new(proj, "Child", parent=parent["id"])
    with pytest.raises(ValueError, match="not finished"):
        taskmod.done(proj, parent["id"])
    # Failed is terminal too: the gate counts "finished", not "Done"
    taskmod.fail(proj, child["id"])
    row = taskmod.done(proj, parent["id"])
    assert row["status"] == "Done"


def test_task_done_not_blocked_by_own_unfinished_state(proj):
    """A task without children can go Done from any state (its own checkboxes
    no longer gate done — that gate is gone)."""
    row, _ = taskmod.new(proj, "Solo")
    assert taskmod.done(proj, row["id"])["status"] == "Done"


def test_plan_review_blocked_by_grandchild(proj):
    """plan gates.tasks covers the whole descendant tree (any depth): a
    grandchild still Draft blocks review — even through an unfinished parent."""
    _plan(proj).new(proj, "P1")
    _plan(proj).transition(proj, "activate", "PLN-0001")
    parent, _ = taskmod.new(proj, "Open task", parent="PLN-0001")
    taskmod.new(proj, "Grandchild", parent=parent["id"])
    with pytest.raises(ValueError, match="not finished"):
        _plan(proj).transition(proj, "review", "PLN-0001")


def test_plan_review_passes_with_failed_child(proj):
    """A Failed child task (terminal) does not block plan review."""
    _plan(proj).new(proj, "P1")
    _plan(proj).transition(proj, "activate", "PLN-0001")
    doomed, _ = taskmod.new(proj, "Doomed", parent="PLN-0001")
    ok, _ = taskmod.new(proj, "Fine", parent="PLN-0001")
    taskmod.fail(proj, doomed["id"])
    taskmod.done(proj, ok["id"])
    row = _plan(proj).transition(proj, "review", "PLN-0001")
    assert row["status"] == "Pending Review"


def test_reopen_restricted_under_closed_ancestor(proj):
    """Once a plan is closed (Pending Review ∈ gates.tasks): re-opening a child
    task, parenting a new task, and moving a task there are all refused."""
    _plan(proj).new(proj, "P1")
    _plan(proj).transition(proj, "activate", "PLN-0001")
    row, _ = taskmod.new(proj, "Child", parent="PLN-0001")
    doomed, _ = taskmod.new(proj, "Doomed child", parent="PLN-0001")
    other, _ = taskmod.new(proj, "Elsewhere")
    taskmod.done(proj, row["id"])
    taskmod.fail(proj, doomed["id"])
    _plan(proj).transition(proj, "review", "PLN-0001")  # Pending Review
    # re-open (Failed → In Progress): refused, the ancestor is closed
    with pytest.raises(ValueError, match="ancestor"):
        _task(proj).transition(proj, "start", doomed["id"])
    with pytest.raises(ValueError, match="closed state"):
        taskmod.new(proj, "Fresh", parent="PLN-0001")
    with pytest.raises(ValueError, match="closed state"):
        taskmod.move(proj, other["id"], "PLN-0001")


def test_task_move_rehomes_and_logs(proj):
    row, _ = taskmod.new(proj, "Mover")  # T-000001, Unparented
    target, _ = taskmod.new(proj, "Home")  # T-000002
    moved = taskmod.move(proj, row["id"], target["id"])
    assert moved["id"] == row["id"]
    db = rec.open_db(proj)
    logs = db.logs(row["id"])
    db.close()
    assert any("parent: None → T-000002" in e["message"] for e in logs)
    # detach back to Unparented
    moved = taskmod.move(proj, row["id"], None)
    db = rec.open_db(proj)
    logs = db.logs(row["id"])
    db.close()
    assert any("→ none" in e["message"] for e in logs)
    tl = (proj.root / "TASKLIST.md").read_text()
    assert "Unparented" in tl and f"{moved['id']}-" in tl


def test_task_move_refuses_cycle_self_missing(proj):
    parent, _ = taskmod.new(proj, "Parent")
    child, _ = taskmod.new(proj, "Child", parent=parent["id"])
    grand, _ = taskmod.new(proj, "Grandchild", parent=child["id"])
    with pytest.raises(ValueError, match="itself"):
        taskmod.move(proj, parent["id"], parent["id"])
    with pytest.raises(ValueError, match="descendant"):
        taskmod.move(proj, parent["id"], grand["id"])
    with pytest.raises(ValueError, match="not found in any record_type"):
        taskmod.move(proj, child["id"], "PLN-9999")


def test_relations_allow_parent_plus_related(proj):
    """PK (source, target, kind): the same record pair may hold a parent edge
    and a related edge at once (no more mutual clobbering); re-adding the same
    edge is idempotent (INSERT OR IGNORE); a SECOND parent edge on one task
    hits the relations_one_parent unique index."""
    import sqlite3
    _plan(proj).new(proj, "P1")
    row, _ = rec.new_record(proj, "task", "Both")
    other, _ = rec.new_record(proj, "task", "Home2")
    db = rec.open_db(proj)
    db.add_relation("PLN-0001", row["id"], "parent")
    db.add_relation(row["id"], "PLN-0001", "related")  # same pair, other kind
    assert db.parent_of(row["id"])["source"] == "PLN-0001"
    assert "PLN-0001" in db.related_of(row["id"])
    db.add_relation("PLN-0001", row["id"], "parent")  # idempotent, no error
    assert db.parent_of(row["id"])["source"] == "PLN-0001"
    db.add_relation("PLN-0001", other["id"], "parent")
    # a second parent edge on the same task: the unique index rejects it
    # (add_relation is INSERT OR IGNORE — verified on the raw connection)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO relations (source, target, kind) VALUES (?,?,?)",
            (row["id"], other["id"], "parent"))
    db.close()
    assert checkmod.check(proj) == []


def test_tasklist_membership_derived(proj):
    """TASKLIST.md is derived: a Draft plan's tasks are hidden, an Active plan
    shows while it has unfinished descendants, and a closed record's finished
    tasks disappear; Unparented only lists unfinished tasks."""
    _plan(proj).new(proj, "P1")
    draft_task, _ = taskmod.new(proj, "Under draft plan", parent="PLN-0001")
    tl = (proj.root / "TASKLIST.md").read_text()
    assert f"{draft_task['id']}-" not in tl  # Draft plan: hidden
    _plan(proj).transition(proj, "activate", "PLN-0001")
    tl = (proj.root / "TASKLIST.md").read_text()
    assert f"{draft_task['id']}-" in tl  # Active plan, unfinished task: shown
    assert "PLN-0001" in tl
    taskmod.done(proj, draft_task["id"])
    tl = (proj.root / "TASKLIST.md").read_text()
    assert f"{draft_task['id']}-" not in tl  # the only task is finished: gone
    # Unparented: unfinished only
    un, _ = taskmod.new(proj, "Ad hoc")
    doomed, _ = taskmod.new(proj, "Doomed ad hoc")
    taskmod.fail(proj, doomed["id"])
    tl = (proj.root / "TASKLIST.md").read_text()
    assert f"{un['id']}-" in tl and f"{doomed['id']}-" not in tl


def test_failed_task_rendered_checked_with_emoji(proj):
    """Terminal statuses render [x]; a non-done terminal status (Failed) gets
    its status emoji before the link; Done gets none. A record section shows
    its FULL subtree (finished tasks included, for progress)."""
    _plan(proj).new(proj, "P1")
    _plan(proj).transition(proj, "activate", "PLN-0001")
    un, _ = taskmod.new(proj, "Open one", parent="PLN-0001")
    doomed, _ = taskmod.new(proj, "Doomed one", parent="PLN-0001")
    taskmod.fail(proj, doomed["id"])
    done, _ = taskmod.new(proj, "Done one", parent="PLN-0001")
    taskmod.done(proj, done["id"])
    tl = (proj.root / "TASKLIST.md").read_text()
    lines = tl.splitlines()
    open_line = next(x for x in lines if f"[{un['id']}-" in x)
    doomed_line = next(x for x in lines if f"[{doomed['id']}-" in x)
    done_line = next(x for x in lines if f"[{done['id']}-" in x)
    assert open_line.strip().startswith("- [ ]")
    assert doomed_line.strip().startswith("- [x] ❌")
    assert done_line.strip().startswith("- [x] [")  # [x] + link, no emoji


def test_fk_rejects_dangling_parent(proj):
    """A parent edge to a nonexistent record is refused by the FK (the
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


def test_task_rejects_ghost_parent_ref(proj):
    with pytest.raises(ValueError, match=r"not found in any record_type"):
        taskmod.new(proj, "T1", parent="999")
    _plan(proj).new(proj, "P1")
    row, _ = taskmod.new(proj, "T1", parent="PLN-0001")
    db = rec.open_db(proj)
    assert db.parent_of(row["id"])["source"] == "PLN-0001"
    db.close()
    taskmod.new(proj, "T2")  # a bare task (no parent) is fine — Unparented


def test_ambiguous_bare_number_is_rejected(proj):
    """Regression (plan P0-1): a bare number that fits SEVERAL record types must
    raise — the widened lookup collects every match, it never silently picks the
    first type. (DEC-0001 is seeded; T-000001/PLN-0001 don't exist yet, so
    create one: "1" then matches DEC-0001 AND T-000001.)"""
    _plan(proj).new(proj, "P1")  # PLN-0001
    taskmod.new(proj, "T1")  # T-000001
    with pytest.raises(ValueError, match="matches several records"):
        taskmod.new(proj, "Amb", parent="1")
    with pytest.raises(ValueError, match="matches several records"):
        taskmod.new(proj, "Amb2", related=["1"])
    # an unambiguous bare number still resolves ("2" → T-000002, the record the
    # refused 'Amb' new left behind)
    row, _ = taskmod.new(proj, "Ok", related=["2"])
    db = rec.open_db(proj)
    assert "T-000002" in db.related_of(row["id"])
    db.close()


def test_state_machine_full_path(proj):
    _plan(proj).new(proj, "P1")
    row, _ = taskmod.new(proj, "T1", parent="PLN-0001")
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


def test_plan_tree_rendered_any_depth(proj):
    """The plan's task tree renders at any depth (no 2-level cap): the
    grandchild sits two indents below the plan root."""
    _plan(proj).new(proj, "P1")
    row, _ = taskmod.new(proj, "Attached", parent="PLN-0001")
    sub, _ = taskmod.new(proj, "Substep", parent=row["id"])
    grand, _ = taskmod.new(proj, "Grandstep", parent=sub["id"])
    plan_md = next((proj.root / "docs/plans").glob("PLN-0001-*.md")).read_text()
    assert "> **subtask:**" not in plan_md  # the old subtask head row is gone
    assert "<summary>Tasks</summary>" in plan_md
    assert f"[{row['id']}-" in plan_md
    assert f"[{sub['id']}-" in plan_md
    assert f"[{grand['id']}-" in plan_md  # third level still rendered
    # the per-record Tasks block is flat (stored/auto lines, id order)
    assert plan_md.count("- [") >= 3


def test_children_rendered_indented_in_main_tasklist(proj):
    """A plan section in TASKLIST.md: the sub-task sits two levels below the
    plan root (1 indent under the section, 2 under the parent task)."""
    _plan(proj).new(proj, "P1")
    _plan(proj).transition(proj, "activate", "PLN-0001")
    parent_row, _ = taskmod.new(proj, "T1", parent="PLN-0001")
    sub_row, _ = taskmod.new(proj, "Substep", parent=parent_row["id"])
    tl = (proj.root / "TASKLIST.md").read_text()
    lines = tl.splitlines()
    plan_line = next(i for i, x in enumerate(lines) if "## " in x and "plan PLN-0001" in x)
    parent_line = next(i for i, x in enumerate(lines) if parent_row["id"] in x)
    sub_line = next(i for i, x in enumerate(lines) if sub_row["id"] in x)
    assert parent_line == plan_line + 2
    assert lines[parent_line].startswith("  - ")
    assert sub_line == parent_line + 1
    assert lines[sub_line].startswith("    - ")


def test_summary_roundtrip(proj):
    """one-sentence summary: stored in the one_sentence column, rendered as an
    italic line in the head fence (no "summary:" label), md wins."""
    row, p = rec.new_record(proj, "task", "S1", summary="kickoff")
    assert row["one_sentence"] == "kickoff"
    text = p.read_text()
    assert "_kickoff_" in text.splitlines()
    p.write_text(text.replace("_kickoff_", "_shipped the core_"))
    rec.sync(proj)
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"])["one_sentence"] == "shipped the core"
    db.close()


def test_logs_limit_env(proj, monkeypatch):
    """PROJ_LOGS: 0 = no changelog block, -1 = all, default = 5, N = latest N."""
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
    monkeypatch.delenv("PROJ_LOGS")  # unset → default 5
    rec.render_all(proj)
    text = p.read_text()
    assert "event 7`" in text and "event 3`" in text
    assert "event 2`" not in text
