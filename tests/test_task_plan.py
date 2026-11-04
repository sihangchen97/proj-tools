"""Phase-1 task tree: gates (descendant tasks, terminal statuses), task move,
derived TASKLIST (no T-000000, no registration), Tasks-block D11 checks."""
import pytest

from proj import check as checkmod
from proj.core import db as dbmod
from proj.engine import item_ops as ops
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.record_types import task as taskmod


def _plan(proj):
    return record_for("plan", rec.load_registry(proj.root))


def _task(proj):
    return record_for("task", rec.load_registry(proj.root))



def test_parent_relation_stored(proj):
    """K2: parent is task→task only — the parent edge is a relations row
    (kind=parent, source=parent task), not a column."""
    parent, _ = taskmod.new(proj, "Parent")
    row, _ = taskmod.new(proj, "Attached", parent=parent["id"])
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert "parent" not in db.get(spec, row["id"])
    assert db.parent_of(row["id"])["source"] == parent["id"]
    assert [c["target"] for c in db.children_of(parent["id"])] == [row["id"]]
    db.close()
    # a plan can NOT be a task's parent anymore
    _plan(proj).new(proj, "P1")
    with pytest.raises(ValueError, match=r"must be a task"):
        taskmod.new(proj, "Nope", parent="PLN-0001")


def test_task_done_blocked_by_draft_child(proj):
    """gates.tasks: a parent task cannot go Done while a descendant (any depth)
    is not in a terminal task status; Done or Failed unblock it."""
    parent, _ = taskmod.new(proj, "Parent")
    taskmod.start(proj, parent["id"])  # S.9: done leaves only In Progress
    child, _ = taskmod.new(proj, "Child", parent=parent["id"])
    with pytest.raises(ValueError, match="not finished"):
        taskmod.done(proj, parent["id"])
    # Failed is terminal too: the gate counts "finished", not "Done"
    taskmod.fail(proj, child["id"])
    row = taskmod.done(proj, parent["id"])
    assert row["status"] == "Done"


def test_task_done_not_blocked_by_own_unfinished_state(proj):
    """A task without children can go Done once it is In Progress (its own
    checkboxes no longer gate done — that gate is gone). S.9: a task with no
    items stays in Draft, so it must be started before it can be done (done
    leaves In Progress only); start first, then done."""
    row, _ = taskmod.new(proj, "Solo")
    taskmod.start(proj, row["id"])
    assert taskmod.done(proj, row["id"])["status"] == "Done"


def test_task_done_blocked_by_grandchild(proj):
    """K2: gates.tasks now lives on TASKS — the whole descendant tree (any depth)
    must be terminal before a parent goes Done. A grandchild still Draft blocks
    the parent, even through an unfinished child."""
    parent, _ = taskmod.new(proj, "Open task")
    taskmod._obj(proj).transition(proj, "start", parent["id"])
    mid, _ = taskmod.new(proj, "Mid", parent=parent["id"])
    taskmod.new(proj, "Grandchild", parent=mid["id"])
    with pytest.raises(ValueError, match="not finished"):
        taskmod.done(proj, parent["id"])


def test_task_done_passes_with_failed_child(proj):
    """K2: a Failed child task (terminal) does not block the parent's Done."""
    parent, _ = taskmod.new(proj, "Parent")
    doomed, _ = taskmod.new(proj, "Doomed", parent=parent["id"])
    ok, _ = taskmod.new(proj, "Fine", parent=parent["id"])
    taskmod.fail(proj, doomed["id"])
    taskmod.start(proj, ok["id"])  # S.9: done leaves only In Progress
    taskmod.done(proj, ok["id"])
    taskmod.start(proj, parent["id"])  # S.9
    row = taskmod.done(proj, parent["id"])
    assert row["status"] == "Done"


def test_reopen_restricted_under_closed_task(proj):
    """K2: once a parent TASK is closed (Done ∈ gates.tasks): re-opening a child
    task, parenting a new task under it, and moving a task there are refused."""
    parent, _ = taskmod.new(proj, "Parent")
    taskmod._obj(proj).transition(proj, "start", parent["id"])
    row, _ = taskmod.new(proj, "Child", parent=parent["id"])
    doomed, _ = taskmod.new(proj, "Doomed child", parent=parent["id"])
    other, _ = taskmod.new(proj, "Elsewhere")
    taskmod.start(proj, row["id"])  # S.9: done leaves only In Progress
    taskmod.done(proj, row["id"])
    taskmod.fail(proj, doomed["id"])
    taskmod.done(proj, parent["id"])  # Done (closed)
    # re-open (Failed → In Progress): refused, the ancestor task is closed
    with pytest.raises(ValueError, match="ancestor"):
        _task(proj).transition(proj, "start", doomed["id"])
    with pytest.raises(ValueError, match="closed state"):
        taskmod.new(proj, "Fresh", parent=parent["id"])
    with pytest.raises(ValueError, match="closed state"):
        taskmod.move(proj, other["id"], parent["id"])


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


def test_task_move_refuses_cycle_self_missing(proj):
    parent, _ = taskmod.new(proj, "Parent")
    child, _ = taskmod.new(proj, "Child", parent=parent["id"])
    grand, _ = taskmod.new(proj, "Grandchild", parent=child["id"])
    with pytest.raises(ValueError, match="itself"):
        taskmod.move(proj, parent["id"], parent["id"])
    with pytest.raises(ValueError, match="descendant"):
        taskmod.move(proj, parent["id"], grand["id"])
    with pytest.raises(ValueError, match="not found \\(types:"):
        taskmod.move(proj, child["id"], "PLN-9999")


@pytest.mark.render_drift
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


def test_failed_task_rendered_checked_with_emoji(proj):
    """K2: the Tasks block renders on a TASK (its task subtree), not a plan.
    Terminal statuses render [x]; a non-done terminal status (Failed) gets its
    status emoji before the link; Done gets none. The full subtree shows
    (finished tasks included, for progress)."""
    from proj.engine.store import file_path, load_registry
    parent, _ = taskmod.new(proj, "Parent")
    taskmod._obj(proj).transition(proj, "start", parent["id"])
    un, _ = taskmod.new(proj, "Open one", parent=parent["id"])
    doomed, _ = taskmod.new(proj, "Doomed one", parent=parent["id"])
    taskmod.fail(proj, doomed["id"])
    done, _ = taskmod.new(proj, "Done one", parent=parent["id"])
    taskmod.start(proj, done["id"])  # S.9: done leaves only In Progress
    taskmod.done(proj, done["id"])
    spec = load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    md = file_path(proj, spec, db.get(spec, parent["id"]))
    db.close()
    lines = md.read_text().splitlines()
    open_line = next(x for x in lines if f"[{un['id']}-" in x)
    doomed_line = next(x for x in lines if f"[{doomed['id']}-" in x)
    done_line = next(x for x in lines if f"[{done['id']}-" in x)
    assert open_line.strip().startswith("- [ ]")
    assert doomed_line.strip().startswith("- [x] ❌")
    assert done_line.strip().startswith("- [x] [")  # [x] + link, no emoji
    # a plan has NO Tasks block (it references work via its Work checklist)
    plan, _ = _plan(proj).new(proj, "P1")
    pdir = proj.root / "docs/plans"
    plan_md = next(pdir.glob(f"{plan['id']}-*.md")).read_text(encoding="utf-8")
    assert "<summary>Tasks</summary>" not in plan_md


@pytest.mark.render_drift
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
    with pytest.raises(ValueError, match=r"not found \(types:"):
        taskmod.new(proj, "T1", parent="999")
    # K2: a plan is not a valid parent (must be a task)
    _plan(proj).new(proj, "P1")
    with pytest.raises(ValueError, match=r"must be a task"):
        taskmod.new(proj, "T1", parent="PLN-0001")
    # a task under a task is fine
    parent, _ = taskmod.new(proj, "P")
    row, _ = taskmod.new(proj, "T1", parent=parent["id"])
    db = rec.open_db(proj)
    assert db.parent_of(row["id"])["source"] == parent["id"]
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
    # B1: a refused `new` (bad/ambiguous ref) leaves NO orphan row — the id
    # space is unchanged, so the next new task is still T-000002
    row, _ = taskmod.new(proj, "Ok")
    assert row["id"] == "T-000002"
    # an unambiguous bare number still resolves ("2" → that same T-000002)
    row2, _ = taskmod.new(proj, "Link", related=["2"])
    db = rec.open_db(proj)
    assert "T-000002" in db.related_of(row2["id"])
    db.close()


@pytest.mark.parametrize(
    ("steps", "expect"),
    [
        (["activate", "review", "shelve", "resume", "review", "archive"], "Archived"),
        (["activate", "review", "shelve", "freeze"], "Archived"),
        (["shelve"], "reject"),  # from Draft: shelve requires Pending Review
    ],
)
def test_plan_state_machine(proj, steps, expect):
    """Table-driven plan state machine (full path + freeze branch + an illegal
    step). e2e ch9 walks the full path against the real CLI."""
    plan = _plan(proj)
    plan.new(proj, "P1")
    for verb in steps:
        if expect == "reject":
            with pytest.raises(ValueError, match="requires"):
                plan.transition(proj, verb, "PLN-0001")
            return
        plan.transition(proj, verb, "PLN-0001")
    spec = rec.load_registry(proj.root)["plan"]
    db = rec.open_db(proj)
    try:
        assert db.get(spec, "PLN-0001")["status"] == expect
    finally:
        db.close()




def test_summary_roundtrip(proj):
    """one-sentence summary: stored in the summary column, rendered as an
    italic line in the head fence (no "summary:" label), md wins."""
    row, p = rec.new_record(proj, "task", "S1", summary="kickoff")
    assert row["summary"] == "kickoff"
    text = p.read_text()
    assert "_kickoff_" in text.splitlines()
    p.write_text(text.replace("_kickoff_", "_shipped the core_"))
    rec.sync(proj)
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    assert db.get(spec, row["id"])["summary"] == "shipped the core"
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


def test_render_all_fetches_task_table_once(proj, many_tasks, monkeypatch):
    """R7: a bulk render reads the TASK table a constant number of times for ALL
    records — not once per rendered record (that was O(N^2): 300 records -> 326
    reads of the task table). The bound is independent of record count."""
    task_reads = 0

    real_all = dbmod.Db.all

    def counting(self, spec):
        nonlocal task_reads
        if spec.record_type == "task":
            task_reads += 1
        return real_all(self, spec)

    monkeypatch.setattr(dbmod.Db, "all", counting)
    rec.render_all(proj)
    # render_all fetches the task rows once (for every record's Tasks block);
    # the TASKLIST view reads it again — a small CONSTANT, never ~N reads.
    assert task_reads <= 3, f"task table read {task_reads}x (expected constant, not per-record)"


def test_mid_command_exception_leaves_no_half_rows(proj, monkeypatch):
    """R3: a write command is one transaction — an exception MIDWAY through it
    (after the insert, before the second statement) leaves NO half-written rows:
    the record is absent, its changelog empty, its relations absent."""
    reg = rec.load_registry(proj.root)
    before = len(rec.open_db(proj).all(reg["decision"]))
    dbmod = __import__("proj.core.db", fromlist=["Db"])

    real_insert = dbmod.Db.insert

    def explode(self, spec, id_, title, status, created, summary="", commit=""):
        real_insert(self, spec, id_, title, status, created, summary, commit)
        raise ValueError("boom mid-command")

    monkeypatch.setattr(dbmod.Db, "insert", explode)
    with pytest.raises(ValueError, match="boom"):
        rec.new_record(proj, "decision", "Half written")
    # the insert rolled back with the rest of the command
    with rec.open_db(proj) as db:
        assert len(db.all(reg["decision"])) == before
        # no changelog row pointing at a record that no longer exists
        assert not db.conn.execute(
            "SELECT 1 FROM record_changelog lc WHERE lc.record_id NOT IN "
            "(SELECT id FROM records)").fetchone()


# ---- P3: the task ↔ item auto-sync (PLAN §10.3 K3 / §26.2) ------------------

def _item_state(proj, rid: str, no: str) -> str:
    db = rec.open_db(proj)
    try:
        r = db.conn.execute(
            "SELECT state FROM items WHERE record_id=? AND no=?",
            (rid, no)).fetchone()
    finally:
        db.close()
    return r["state"]


def test_p3_task_done_closes_referencing_item(proj):
    """Forward sync: the task goes Done → every live item referencing it
    (→ T-…) is set to done, with the cause logged on the REFERRING record."""
    t, _ = taskmod.new(proj, "Rerun eval")
    taskmod._obj(proj).transition(proj, "start", t["id"])
    inc, _ = record_for("incident", rec.load_registry(proj.root)).new(proj, "Eval flake")
    ops.add(proj, inc["id"], "refile after the rerun", task=t["id"])
    assert _item_state(proj, inc["id"], "001") == "open"
    taskmod.done(proj, t["id"])
    assert _item_state(proj, inc["id"], "001") == "done"
    # the cause is logged on the incident side, not a bare `edited` line
    db = rec.open_db(proj)
    try:
        logs = [e for e in db.logs(inc["id"])
                if "item #001" in e["message"] and "auto:" in e["message"]]
    finally:
        db.close()
    assert logs and f"→ done (auto: {t['id']} Done" in logs[0]["message"]
    # the incident md re-rendered: the item shows its closed emoji + the ref
    p = next((proj.root / "docs" / "incidents").glob(f"{inc['id']}-*.md"))
    inc_md = p.read_text(encoding="utf-8")
    assert f"- [x] #001 ✅ refile after the rerun → {t['id']}" in inc_md


def test_p3_auto_close_event_carries_date(proj):
    """Root cause of `proj check` tables-differ:items: the P3 auto-close used to
    emit an `item.set` WITHOUT a `date`, so a replay stamped closed_at from the
    wall clock — every replay differed and check could never pass. The auto-close
    event now carries its date, so the log reproduces the DB exactly."""
    t, _ = taskmod.new(proj, "Rerun")
    taskmod._obj(proj).transition(proj, "start", t["id"])
    inc, _ = record_for("incident", rec.load_registry(proj.root)).new(proj, "Eval flake")
    ops.add(proj, inc["id"], "refile after the rerun", task=t["id"])
    # the item's uuid (stable identity for the event log) before it closes
    db = rec.open_db(proj)
    try:
        item_uuid = db.conn.execute(
            "SELECT uuid FROM items WHERE record_id=? AND no='001'",
            (inc["id"],)).fetchone()["uuid"]
        assert item_uuid
    finally:
        db.close()
    taskmod.done(proj, t["id"])
    assert _item_state(proj, inc["id"], "001") == "done"
    from proj import events as ev
    events = ev.read(proj.root / ".proj" / "events.jsonl")
    close = [e for e in events
             if e["op"] == "item.set" and e.get("item") == item_uuid
             and e.get("fields", {}).get("state") == "done"]
    assert close, "the auto-close item.set event is missing from the log"
    assert close[0].get("date"), "the auto-close event must carry its date"


def test_p3_task_failed_marks_item_failed(proj):
    t, _ = taskmod.new(proj, "Deploy")
    taskmod._obj(proj).transition(proj, "start", t["id"])
    inc, _ = record_for("incident", rec.load_registry(proj.root)).new(proj, "Outage")
    ops.add(proj, inc["id"], "roll forward once deploy is safe", task=t["id"])
    taskmod.fail(proj, t["id"])
    assert _item_state(proj, inc["id"], "001") == "failed"


def test_p3_reopening_task_reopens_item(proj):
    """A task moved back to a non-terminal status reopens the item it closed."""
    t, _ = taskmod.new(proj, "Rerun")
    taskmod._obj(proj).transition(proj, "start", t["id"])
    inc, _ = record_for("incident", rec.load_registry(proj.root)).new(proj, "Flake")
    ops.add(proj, inc["id"], "re-check after the rerun", task=t["id"])
    taskmod.done(proj, t["id"])
    assert _item_state(proj, inc["id"], "001") == "done"
    # move it back to a running (non-terminal) status: the item reopens
    rec.status_set(proj, "task", t["id"], "In Progress")
    assert _item_state(proj, inc["id"], "001") == "open"


@pytest.mark.render_drift
def test_p3_reverse_gate_blocks_manual_close(proj):
    """Reverse gate: you cannot MANUALLY close an item whose task is still not
    terminal — clear the ref (or finish the task) first."""
    t, _ = taskmod.new(proj, "Rerun")
    taskmod._obj(proj).transition(proj, "start", t["id"])  # still In Progress
    inc, _ = record_for("incident", rec.load_registry(proj.root)).new(proj, "Flake")
    ops.add(proj, inc["id"], "re-check", task=t["id"])
    with pytest.raises(Exception, match=r"still In Progress"):
        ops.set_state(proj, f"{inc['id']}#001", "done")
    # the item is unchanged (the refused op left it open)
    assert _item_state(proj, inc["id"], "001") == "open"
    # but closing is fine once the ref is cleared
    ops.edit(proj, f"{inc['id']}#001", task="")
    ops.set_state(proj, f"{inc['id']}#001", "done")
    assert _item_state(proj, inc["id"], "001") == "done"


def test_p3_auto_close_triggers_promote(proj):
    """A referenced task Done closes the item; that first closed item then
    promotes the record Draft → In Progress (promote rides the auto close)."""
    t, _ = taskmod.new(proj, "Rerun")
    taskmod._obj(proj).transition(proj, "start", t["id"])
    # a debt whose Repayment Plan references the task
    debt, _ = record_for("debt", rec.load_registry(proj.root)).new(proj, "Slow path")
    assert debt["status"] == "Draft"
    ops.add(proj, debt["id"], "profile the hot loop", task=t["id"])
    assert _item_state(proj, debt["id"], "001") == "open"
    taskmod.done(proj, t["id"])
    assert _item_state(proj, debt["id"], "001") == "done"
    assert debt["id"] is not None
    db = rec.open_db(proj)
    try:
        assert db.get(rec.load_registry(proj.root)["debt"], debt["id"])["status"] == "In Progress"
    finally:
        db.close()


# ---- B1: a bad reference never leaves an orphan row (PLAN §28.1 22:45) ------

def _no_orphan(proj, rid: str) -> bool:
    """True if no record row exists for rid (B1: a refused new leaves nothing)."""
    db = rec.open_db(proj)
    try:
        reg = rec.load_registry(proj.root)
        return all(db.get(spec, rid) is None for spec in reg.values())
    finally:
        db.close()


def test_bad_parent_leaves_no_orphan(proj):
    """B1: `task new --parent <nonexistent>` validates the ref before the row is
    created — the error leaves no orphan task row (or md) behind."""
    before = len(list((proj.root / "docs/tasks").glob("*.md")))
    with pytest.raises(ValueError, match="not found"):
        taskmod.new(proj, "Orphan", parent="T-999999")
    after = len(list((proj.root / "docs/tasks").glob("*.md")))
    assert after == before, "a refused parent new left an orphan md file"
    db = rec.open_db(proj)
    spec = rec.load_registry(proj.root)["task"]
    assert db.get(spec, "T-000001") is None  # no T-000001 row created
    db.close()


def test_bad_related_leaves_no_orphan(proj):
    """B1: a bad `related` ref raises before the row exists (no orphan)."""
    with pytest.raises(ValueError, match="does not match any record"):
        taskmod.new(proj, "Orphan", related=["T-999999"])
    assert _no_orphan(proj, "T-000001")
    db = rec.open_db(proj)
    spec = rec.load_registry(proj.root)["task"]
    assert db.get(spec, "T-000001") is None
    db.close()


def test_bad_supersede_leaves_no_orphan(proj):
    """B1: a bad `supersede` ref raises before the row exists (no orphan)."""
    reg = rec.load_registry(proj.root)
    before = len(rec.open_db(proj).all(reg["decision"]))
    # decision is a same-type-supersede-able record_type; DEC-9999 does not exist
    with pytest.raises(ValueError, match="does not match any record"):
        record_for("decision", reg).new(proj, "Orphan", supersede=["DEC-9999"])
    assert len(rec.open_db(proj).all(reg["decision"])) == before  # no orphan row
