"""C6: the new `proj check` items (hard-parts C6) — item→task ref integrity,
P3 item↔task drift (a merge can leave a closed task with an open referencing
item; replay does not run business logic, so check reports, nobody auto-fixes),
and replay↔cache consistency (the committed log must reproduce the DB).

Fixtures build states the way a merge would: a task's Acceptance-Criteria item
references another task (→ T-…), then the referenced task is closed/deleted via
raw DB writes (bypassing the P3 auto-sync the write path would run) — exactly
the divergence a cross-clone merge produces.
"""
from __future__ import annotations

from proj import check
from proj import events as ev
from proj import replay as rp
from proj.core import db as dbmod
from proj.engine import item_ops
from proj.engine import store as rec


def _db(proj) -> dbmod.Db:
    return dbmod.Db(proj.db_path)


def _two_tasks(proj) -> tuple[str, str]:
    """T-000001 (referenced) + T-000002 (holder). Returns (ref_id, holder_id)."""
    ref, _ = rec.new_record(proj, "task", "Build it")
    hold, _ = rec.new_record(proj, "task", "Ship it")
    return ref["id"], hold["id"]


def _item_refers(proj, holder_id: str, task_id: str) -> None:
    item_ops.add(proj, holder_id, "the build",
                 section="Acceptance Criteria", task=task_id)


def _set_task_status(proj, task_id: str, status: str) -> None:
    """Set a task's status directly (bypassing the write path, which would run
    the P3 auto-sync — exactly the state a cross-clone merge produces)."""
    reg = rec.load_registry(proj.root)
    db = _db(proj)
    try:
        with db:
            db.set(reg["task"], task_id, status=status)
    finally:
        db.close()
    rec.render_all(proj)


def _strip_seed(proj) -> None:
    """Drop the seed decision (row + log + md) so a hand-written log can fully
    cover the cache — a CLI-created record has no events of its own (pre-C2)."""
    db = _db(proj)
    try:
        with db:
            db.conn.execute("DELETE FROM record_changelog WHERE record_id=?",
                            ("DEC-0001",))
            db.conn.execute("DELETE FROM items WHERE record_id=?", ("DEC-0001",))
            db.conn.execute("DELETE FROM records WHERE id=?", ("DEC-0001",))
    finally:
        db.close()
    spec = rec.load_registry(proj.root)["decision"]
    for p in (proj.root / spec.dir).glob("DEC-0001-*.md"):
        p.unlink()


def test_check_flags_dangling_item_task_ref(proj):
    ref_id, holder_id = _two_tasks(proj)
    _item_refers(proj, holder_id, ref_id)
    db = _db(proj)
    try:
        with db:
            db.conn.execute(
                "DELETE FROM record_changelog WHERE record_id=?", (ref_id,))
            db.conn.execute("DELETE FROM items WHERE record_id=?", (ref_id,))
            db.conn.execute("DELETE FROM records WHERE id=?", (ref_id,))
    finally:
        db.close()
    rec.render_all(proj)
    problems = check.check(proj)
    dangling = [p for p in problems if "references task" in p]
    assert dangling, problems
    assert ref_id in dangling[0] and holder_id in dangling[0]
    assert "proj item edit" in dangling[0]


def test_check_flags_p3_drift_when_task_closed_item_open(proj):
    ref_id, holder_id = _two_tasks(proj)
    _item_refers(proj, holder_id, ref_id)
    _set_task_status(proj, ref_id, "Done")
    problems = check.check(proj)
    drift = [p for p in problems if "out of" in p and "sync" in p]
    assert drift, problems
    assert ref_id in drift[0] and holder_id in drift[0]
    assert "proj item set" in drift[0] and "done" in drift[0]


def test_check_is_silent_when_p3_in_sync(proj):
    ref_id, holder_id = _two_tasks(proj)
    _item_refers(proj, holder_id, ref_id)
    _set_task_status(proj, ref_id, "Done")
    # a healthy clone: the item followed its task (what the write path does)
    db = _db(proj)
    try:
        with db:
            db.conn.execute(
                "UPDATE items SET state='done', closed_at=? WHERE task=?",
                (dbmod.utc_now(), ref_id))
    finally:
        db.close()
    rec.render_all(proj)
    problems = check.check(proj)
    assert not [p for p in problems if "references" in p
                or "out of" in p], problems


def test_check_replay_consistency_healthy_and_broken_log(proj):
    """Replay the committed log onto an empty DB and compare the state."""
    _strip_seed(proj)
    a = "2026-01-01T00:00:00+00:00"
    u1, i1 = "a" * 32, "b" * 32
    events = [
        ev.make("create", actor="a", at=1, seq=1, rec=u1, type="task",
                id="T-000001", title="A", status="Draft", summary="",
                created=a, sections={}),
        ev.make("item.add", actor="a", at=2, seq=1, rec=u1, item=i1,
                no="001", text="s", state="open",
                section="Acceptance Criteria", created=a),
        ev.make("set", actor="a", at=3, seq=1, rec=u1,
                fields={"title": "A2"}, date=a),
    ]
    db = _db(proj)
    try:
        with db:
            rp.replay(db, events, rec.load_registry(proj.root))
    finally:
        db.close()
    ev_path = proj.root / ".proj" / "events.jsonl"
    ev_path.write_text("".join(ev.to_line(e) for e in events), encoding="utf-8")
    rec.render_all(proj)

    problems = check.check(proj)
    assert not [p for p in problems if "events.jsonl" in p], problems

    # break the log (drop the title change) while the DB keeps it: the replay
    # no longer reproduces the cache (to_line is compact JSON, no spaces)
    lines = ev_path.read_text(encoding="utf-8").splitlines(keepends=True)
    ev_path.write_text("".join(line for line in lines if '"title":"A2"' not in line),
                       encoding="utf-8")
    problems = check.check(proj)
    mismatch = [p for p in problems if "does not reproduce" in p]
    assert mismatch, problems
    assert "records" in mismatch[0]

    # a git conflict marker is reported at the line level, and the replay is
    # skipped (it would abort on the bad line) — no crash either way
    ev_path.write_text(
        ev_path.read_text(encoding="utf-8")
        + "<<<<<<< refs/heads/remote\n{\"at\": 4}\n>>>>>>> refs/heads/remote\n",
        encoding="utf-8")
    problems = check.check(proj)
    assert any("git conflict marker" in p for p in problems), problems
    assert not [p for p in problems if "does not reproduce" in p], problems


def test_check_without_events_file_is_healthy(proj):
    # pre-C2 projects record no events at all: the replay check is skipped and
    # the seed project stays OK
    problems = check.check(proj)
    assert not [p for p in problems if "events" in p.lower()], problems
