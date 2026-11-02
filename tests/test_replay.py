"""E2 (hard-parts-impl §0.1): replay.py - the DB as a deterministic function
of the event log (C3.1). Hand-written event sequences: determinism, id and
item renumbering, LWW overwrites, dangling refs, deletes, project changelog.

The replayer only reads the existing Db interface (C1 uuid columns do not
exist yet: record uuids live in the replay's own mapping, item uuids ride as
a temporary `_uuid` row field that items_save never persists).
"""
from __future__ import annotations

import random
import string
from pathlib import Path

from proj import events as ev
from proj import replay as rp
from proj.core import db as dbmod
from proj.core.templates import load_registry


def _uuid() -> str:
    return "".join(random.choices(string.hexdigits, k=32))


def _empty_db(tmp_path: Path):
    return dbmod.Db(tmp_path / "replay.sqlite")


def _create(uuid, type_, id_, title, at, actor, seq=1, **extra):
    base = {"actor": actor, "at": at, "seq": seq, "rec": uuid,
            "type": type_, "id": id_, "title": title, "status": "Draft",
            "summary": "", "sections": {}}
    base.update(extra)
    return ev.make("create", **base)


def test_create_applies_record(tmp_path):
    db, res = _run(tmp_path, [_create(_uuid(), "task", "T-000001", "one",
                                       1_000, "alex")])
    rows = db.conn.execute("SELECT id, title, status FROM records").fetchall()
    assert [dict(r)["id"] for r in rows] == ["T-000001"]
    assert rows[0]["title"] == "one"
    assert res.renamed == []


def test_determinism_regardless_of_line_order(tmp_path):
    ua, ub = _uuid(), _uuid()
    mk = [_create(ua, "task", "T-000001", "a", 1_000, "alex"),
          _create(ub, "task", "T-000002", "b", 2_000, "billy"),
          ev.make("set", actor="billy", at=3_000, seq=1, rec=ub,
                  fields={"title": "b2"})]
    a = _db_tables(_run(tmp_path / "a", mk))
    b = _db_tables(_run(tmp_path / "b", list(reversed(mk))))
    assert a == b  # same lines, different file order -> identical DB


def test_id_collision_renumbers_the_later_one(tmp_path):
    ua, ub = _uuid(), _uuid()
    mk = [_create(ua, "task", "T-000005", "a", 1_000, "alex"),
          _create(ub, "task", "T-000005", "b", 2_000, "billy")]
    db, res = _run(tmp_path, mk)
    assert res.ids[ua] == "T-000005" and res.ids[ub] == "T-000006"
    assert res.renamed == [{"uuid": ub, "proposed": "T-000005",
                            "actual": "T-000006", "collides_with": ua}]
    assert [r["id"] for r in db.conn.execute(
        "SELECT id FROM records ORDER BY id")] == ["T-000005", "T-000006"]
    assert db.get(_task_spec(), "T-000006")["title"] == "b"


def test_lww_same_field_different_actors_keeps_later_and_reports(tmp_path):
    u = _uuid()
    mk = [_create(u, "task", "T-000001", "t0", 1_000, "alex"),
          ev.make("set", actor="alex", at=2_000, seq=1, rec=u,
                  fields={"title": "A"}),
          ev.make("set", actor="billy", at=3_000, seq=1, rec=u,
                  fields={"title": "B"})]
    db, res = _run(tmp_path, mk)
    assert db.get(_task_spec(), "T-000001")["title"] == "B"
    assert res.overwrites == [{
        "record": u, "field": "title", "overwritten_value": "A",
        "overwritten_by": "alex", "kept_value": "B", "kept_by": "billy"}]


def test_lww_different_fields_both_kept(tmp_path):
    u = _uuid()
    mk = [_create(u, "task", "T-000001", "t0", 1_000, "alex"),
          ev.make("set", actor="alex", at=2_000, seq=1, rec=u,
                  fields={"title": "A"}),
          ev.make("set", actor="billy", at=3_000, seq=1, rec=u,
                  fields={"status": "Done"})]
    db, res = _run(tmp_path, mk)
    row = db.get(_task_spec(), "T-000001")
    assert row["title"] == "A" and row["status"] == "Done"
    assert res.overwrites == []


def test_section_set_roundtrip(tmp_path):
    u = _uuid()
    mk = [_create(u, "plan", "PLN-0001", "p", 1_000, "alex",
                  sections={"Goal": "g0"}),
          ev.make("section.set", actor="alex", at=2_000, seq=1, rec=u,
                  name="Goal", text="g1")]
    db, _ = _run(tmp_path, mk)
    import json
    row = db.get(_plan_spec(), "PLN-0001")
    assert json.loads(row["sections"])["Goal"] == "g1"


def test_item_add_collision_renumbers_within_record(tmp_path):
    u = _uuid()
    ia, ib = _uuid(), _uuid()
    mk = [_create(u, "task", "T-000001", "t", 1_000, "alex"),
          ev.make("item.add", actor="alex", at=2_000, seq=1, rec=u,
                  item=ia, no="001", section="Acceptance Criteria",
                  text="a"),
          ev.make("item.add", actor="billy", at=3_000, seq=1, rec=u,
                  item=ib, no="001", section="Acceptance Criteria",
                  text="b")]
    db, res = _run(tmp_path, mk)
    rows = db.items_load("T-000001")
    assert [r["no"] for r in rows] == ["001", "002"]
    assert [r["text"] for r in rows] == ["a", "b"]
    assert res.renamed == [{"item": ib, "proposed": "001", "actual": "002",
                            "record": "T-000001"}]


def test_item_delete_then_modify_stays_deleted(tmp_path):
    u = _uuid()
    i = _uuid()
    mk = [_create(u, "task", "T-000001", "t", 1_000, "alex"),
          ev.make("item.add", actor="alex", at=2_000, seq=1, rec=u,
                  item=i, no="001", section="Acceptance Criteria", text="x"),
          ev.make("item.set", actor="alex", at=3_000, seq=1, rec=u, item=i,
                  fields={"deleted": True}),
          ev.make("item.set", actor="billy", at=4_000, seq=1, rec=u, item=i,
                  fields={"text": "changed"})]
    db, _ = _run(tmp_path, mk)
    (row,) = db.items_load("T-000001")
    assert row["deleted"] == 1 and row["text"] == "changed"


def test_item_set_state_done_stamps_closed_at(tmp_path):
    u = _uuid()
    i = _uuid()
    mk = [_create(u, "task", "T-000001", "t", 1_000, "alex"),
          ev.make("item.add", actor="alex", at=2_000, seq=1, rec=u,
                  item=i, no="001", section="Acceptance Criteria", text="x"),
          ev.make("item.set", actor="alex", at=5_000, seq=1, rec=u, item=i,
                  fields={"state": "done"}, date="2026-01-02T03:04:05+00:00")]
    db, _ = _run(tmp_path, mk)
    (row,) = db.items_load("T-000001")
    assert row["state"] == "done" and row["closed_at"] == "2026-01-02T03:04:05+00:00"


def test_item_move_renumbers_and_tombstones(tmp_path):
    u = _uuid()
    ia, ib = _uuid(), _uuid()
    mk = [_create(u, "task", "T-000001", "t", 1_000, "alex"),
          ev.make("item.add", actor="alex", at=2_000, seq=1, rec=u,
                  item=ia, no="A01", section="Acceptance Criteria", text="a"),
          ev.make("item.add", actor="alex", at=2_500, seq=2, rec=u,
                  item=ib, no="001", section="Acceptance Criteria", text="b"),
          ev.make("item.move", actor="alex", at=3_000, seq=3, rec=u, item=ib,
                  no="B01", section="Acceptance Criteria")]
    db, res = _run(tmp_path, mk)
    rows = db.items_load("T-000001")
    nos = {r["no"] for r in rows}
    assert "001" in nos and any(r["deleted"] == 1 and r["no"] == "001"
                                for r in rows)
    moved = next(r for r in rows if r["no"] == "B01")
    assert moved["deleted"] == 0 and moved["text"] == "b"
    assert any(r["item"] == ib and r["proposed"] == "001" and r["actual"] == "B01"
               for r in res.renamed)


def test_dangling_task_ref_is_skipped_with_diagnostic(tmp_path):
    u = _uuid()
    i = _uuid()
    ghost = _uuid()
    mk = [_create(u, "task", "T-000001", "t", 1_000, "alex"),
          ev.make("item.add", actor="alex", at=2_000, seq=1, rec=u,
                  item=i, no="001", section="Acceptance Criteria", text="x",
                  task=ghost)]
    db, res = _run(tmp_path, mk)
    (row,) = db.items_load("T-000001")
    assert row["task"] == ""
    assert any(f"uuid {ghost}" in d for d in res.diagnostics)


def test_dangling_relation_is_skipped(tmp_path):
    u = _uuid()
    ghost = _uuid()
    mk = [_create(u, "task", "T-000001", "t", 1_000, "alex"),
          ev.make("rel.add", actor="alex", at=2_000, seq=1, rec=u,
                  kind="related", target=ghost)]
    db, res = _run(tmp_path, mk)
    assert db.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
    assert res.diagnostics


def test_delete_cascades_and_removes_wip(tmp_path):
    u = _uuid()
    i = _uuid()
    mk = [_create(u, "task", "T-000001", "t", 1_000, "alex"),
          ev.make("item.add", actor="alex", at=2_000, seq=1, rec=u,
                  item=i, no="001", section="Acceptance Criteria", text="x"),
          ev.make("wip.add", actor="alex", at=3_000, seq=1, rec=u,
                  source="manual"),
          ev.make("delete", actor="alex", at=4_000, seq=1, rec=u)]
    db, _ = _run(tmp_path, mk)
    assert db.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM wip_entries").fetchone()[0] == 0


def test_unknown_record_uuid_is_diagnosed_not_fatal(tmp_path):
    mk = [ev.make("set", actor="alex", at=1_000, seq=1, rec=_uuid(),
                  fields={"title": "x"})]
    db, res = _run(tmp_path, mk)
    assert db.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert res.diagnostics


def test_project_changelog_add_and_release(tmp_path):
    mk = [ev.make("changelog.add", actor="alex", at=1_000, seq=1, rec="",
                  category="Added", message="m1"),
          ev.make("changelog.add", actor="alex", at=2_000, seq=1, rec="",
                  category="Changed", message="m2"),
          ev.make("changelog.release", actor="alex", at=3_000, seq=1, rec="",
                  version="v1.0.0")]
    db, _ = _run(tmp_path, mk)
    got = db.cl_entries("v1.0.0")
    assert [(r["category"], r["message"]) for r in got] == [
        ("Added", "m1"), ("Changed", "m2")]


def test_dedupe_identical_lines(tmp_path):
    u = _uuid()
    mk = [_create(u, "task", "T-000001", "t", 1_000, "alex"),
          ev.make("set", actor="alex", at=2_000, seq=1, rec=u,
                  fields={"title": "A"})]
    db, _res = _run(tmp_path, mk + mk[:1])  # a union-merge duplicate line
    assert db.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    assert db.get(_task_spec(), "T-000001")["title"] == "A"


# ---------------------------------------------------------------------------


def _task_spec():
    return next(s for s in load_registry(None).values()
                if s.record_type == "task")


def _plan_spec():
    return next(s for s in load_registry(None).values()
                if s.record_type == "plan")


def _run(tmp_path: Path, events: list[dict]) -> tuple:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = _empty_db(tmp_path)
    try:
        res = rp.replay(db, events, {s.record_type: s
                                      for s in load_registry(None).values()})
    except Exception:
        db.close()
        raise
    return db, res


def _db_tables(run) -> dict:
    db, _ = run
    out = {}
    for table in ("records", "record_changelog", "items", "relations",
                  "wip_entries", "changelog_entries"):
        out[table] = [dict(r) for r in db.conn.execute(
            f"SELECT * FROM {table} ORDER BY 1, 2")]
    db.close()
    return out
