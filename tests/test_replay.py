"""E2 (hard-parts-impl §0.1): replay.py - the DB as a deterministic function
of the event log (C3.1). Hand-written event sequences: determinism, id and
item renumbering, LWW overwrites, dangling refs, deletes, project changelog.

The replayer only reads the existing Db interface (C1 uuid columns do not
exist yet: record uuids live in the replay's own mapping, item uuids ride as
a temporary `_uuid` row field that items_save never persists).
"""
from __future__ import annotations

import json
import random
import string
import tempfile
from pathlib import Path

import pytest

from proj import events as ev
from proj import replay as rp
from proj.core import db as dbmod
from proj.core.templates import load_registry


def _uuid() -> str:
    return "".join(random.choices(string.hexdigits, k=32))


def _empty_db(tmp_path: Path):
    return dbmod.Db(tmp_path / "replay.sqlite")


def _db_at(file_path: Path) -> dbmod.Db:
    return dbmod.Db(file_path)


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

# ---------------------------------------------------------------------------
# E5 (C3.2): incremental replay — plan_incremental / apply_incremental


def _specs():
    return {s.record_type: s for s in load_registry(None).values()}


def test_plan_noop_when_size_unchanged(tmp_path):
    p = tmp_path / "events.jsonl"
    u = _uuid()
    ev.append(p, [_create(u, "task", "T-000001", "a", 1_000, "alex")])
    meta = rp.commit_meta(_empty_db(tmp_path), p)
    got = rp.plan_incremental(p, meta)
    assert got[0] == "noop"


def test_plan_append_on_pure_append(tmp_path):
    p = tmp_path / "events.jsonl"
    ua, ub = _uuid(), _uuid()
    ev.append(p, [_create(ua, "task", "T-000001", "a", 1_000, "alex")])
    meta = rp.commit_meta(_empty_db(tmp_path), p)
    new = _create(ub, "task", "T-000002", "b", 2_000, "billy")
    ev.append(p, [new])
    kind, lines = rp.plan_incremental(p, meta)
    assert kind == "append" and lines == [new]


def test_plan_full_when_a_new_line_sorts_earlier(tmp_path):
    """A merge can insert a line whose sort key is earlier than ev:last_key
    (the other clone wrote it with a smaller HLC) -> full replay (C3.2)."""
    p = tmp_path / "events.jsonl"
    ua = _uuid()
    ev.append(p, [_create(ua, "task", "T-000001", "a", 1_000, "alex")])
    meta = rp.commit_meta(_empty_db(tmp_path), p)
    # a new line with at < the applied 1_000 -> sorts earlier
    ev.append(p, [_create(_uuid(), "task", "T-000002", "b", 500, "billy")])
    kind, reason = rp.plan_incremental(p, meta)
    assert kind == "full" and "sorts before" in reason


def test_plan_full_when_prefix_changed(tmp_path):
    p = tmp_path / "events.jsonl"
    ua = _uuid()
    ev.append(p, [_create(ua, "task", "T-000001", "a", 1_000, "alex")])
    meta = rp.commit_meta(_empty_db(tmp_path), p)
    raw = p.read_bytes()
    # rewrite byte 40 of the prefix, then append (rebase / revert)
    p.write_bytes(raw[:40] + bytes([raw[40] + 1]) + raw[41:] +
                  ev.to_line(_create(_uuid(), "task", "T-000002", "b",
                                     2_000, "billy")).encode("utf-8"))
    kind, reason = rp.plan_incremental(p, meta)
    assert kind == "full" and "prefix changed" in reason


def test_plan_full_when_truncated(tmp_path):
    p = tmp_path / "events.jsonl"
    ua = _uuid()
    ev.append(p, [_create(ua, "task", "T-000001", "a", 1_000, "alex")])
    meta = rp.commit_meta(_empty_db(tmp_path), p)
    p.write_bytes(p.read_bytes()[:10])
    kind, reason = rp.plan_incremental(p, meta)
    assert kind == "full" and "shrank" in reason


def test_plan_full_when_missing_trailing_newline(tmp_path):
    p = tmp_path / "events.jsonl"
    ua = _uuid()
    ev.append(p, [_create(ua, "task", "T-000001", "a", 1_000, "alex")])
    meta = rp.commit_meta(_empty_db(tmp_path), p)
    new = _create(_uuid(), "task", "T-000002", "b", 2_000, "billy")
    p.write_bytes(p.read_bytes() +
                  ev.to_line(new).rstrip("\n").encode("utf-8"))
    kind, reason = rp.plan_incremental(p, meta)
    assert kind == "full" and "newline" in reason


def test_apply_incremental_equals_full_replay(tmp_path):
    """The core property: a pure append applied incrementally to a cached db
    yields the SAME tables as a full replay of every line on an empty db."""
    ua, ub = _uuid(), _uuid()
    mk = [_create(ua, "task", "T-000001", "a", 1_000, "alex"),
          ev.make("item.add", actor="alex", at=2_000, seq=1, rec=ua,
                  item=_uuid(), no="001", section="Acceptance Criteria",
                  text="x"),
          _create(ub, "task", "T-000002", "b", 3_000, "billy"),
          ev.make("set", actor="billy", at=4_000, seq=1, rec=ub,
                  fields={"title": "b2"})]
    p = tmp_path / "events.jsonl"
    ev.append(p, mk[:2])
    (tmp_path / "a").mkdir()
    db = _db_at(tmp_path / "a" / "replay.sqlite")
    rp.replay(db, mk[:2], _specs())          # the cached state of clone A
    meta = rp.commit_meta(db, p)
    ev.append(p, mk[2:])
    kind, lines = rp.plan_incremental(p, meta)
    assert kind == "append"
    res_inc = rp.apply_incremental(db, lines, _specs())
    # compare against a full replay of all four lines on a fresh empty db
    (tmp_path / "b").mkdir()
    db2 = _db_at(tmp_path / "b" / "replay.sqlite")
    rp.replay(db2, mk, _specs())
    got = _norm(_db_tables((db, None)))
    ref = _norm(_db_tables((db2, None)))
    assert got == ref
    assert not res_inc.diagnostics


def _norm(tables: dict) -> dict:
    """Normalize the auto timestamps that differ between the incremental and
    full paths (created / a freshly-logged date) before comparing tables."""
    for rows in tables.values():
        for r in rows:
            if "created" in r:
                r.pop("created")
            if "date" in r and str(r["date"]).startswith("20"):
                r["date"] = "<now>"
    return tables

# ---------------------------------------------------------------------------
# E6 (C6 compact): snapshots — snapshot_events + the `snapshot` op


def _mk_rich():
    """A log that exercises every table: 3 records, a parent edge, a related
    edge, supersede, WIP, items (incl. a tombstone + a closed row), log rows
    and a project changelog."""
    ut, up, uc = _uuid(), _uuid(), _uuid()
    i1, i2 = _uuid(), _uuid()
    return [
        _create(ut, "task", "T-000001", "子", 1_000, "alex"),
        _create(up, "task", "T-000002", "父", 2_000, "alex"),
        _create(uc, "decision", "DEC-0001", "决定", 3_000, "billy",
                summary="s"),
        ev.make("rel.add", actor="alex", at=4_000, seq=1, rec=up,
                kind="parent", target=ut),
        ev.make("rel.add", actor="billy", at=5_000, seq=1, rec=uc,
                kind="related", target=up),
        ev.make("item.add", actor="alex", at=6_000, seq=1, rec=ut,
                item=i1, no="001", section="Acceptance Criteria", text="a"),
        ev.make("item.add", actor="alex", at=7_000, seq=2, rec=ut,
                item=i2, no="002", section="Acceptance Criteria", text="b"),
        ev.make("item.set", actor="alex", at=8_000, seq=3, rec=ut, item=i1,
                fields={"state": "done"}, date="2026-01-02T03:04:05+00:00"),
        ev.make("item.set", actor="billy", at=9_000, seq=4, rec=ut, item=i2,
                fields={"deleted": True}),
        ev.make("wip.add", actor="alex", at=10_000, seq=5, rec=ut,
                source="manual"),
        ev.make("log", actor="alex", at=11_000, seq=6, rec=ut,
                message="T-000001 Draft -> In Progress",
                status="In Progress", date="2026-01-02T03:00:01+00:00"),
        ev.make("changelog.add", actor="billy", at=12_000, seq=7, rec="",
                category="Added", message="m", date="2026-01-02T03:00:02+00:00"),
    ]


def _norm_tables(db, keep=("created",)):
    t = _db_tables((db, None))
    for rows in t.values():
        for r in rows:
            for k in ("created", "date", "closed_at", "added_at"):
                if k in r and str(r[k]).startswith("20"):
                    r[k] = "<ts>"
    return t


def test_snapshot_replay_equals_original(tmp_path):
    """C6: replaying the original log and replaying snapshot_events(db) give
    identical tables (ignoring auto timestamps)."""
    mk = _mk_rich()
    (tmp_path / "a").mkdir()
    db = _db_at(tmp_path / "a" / "db.sqlite")
    rp.replay(db, mk, _specs())
    snap = rp.snapshot_events(db)
    assert [e["op"] for e in snap].count("snapshot") == 4  # 3 records + 1 proj
    (tmp_path / "b").mkdir()
    db2 = _db_at(tmp_path / "b" / "db.sqlite")
    res = rp.replay(db2, snap, _specs())
    assert not res.diagnostics
    assert _norm_tables(db2) == _norm_tables(db)
    db.close()


def test_events_after_snapshot_still_apply(tmp_path):
    """C6: an event appended after the snapshot events (larger HLC) changes
    exactly what it says and nothing else."""
    mk = _mk_rich()
    (tmp_path / "a").mkdir()
    db = _db_at(tmp_path / "a" / "db.sqlite")
    rp.replay(db, mk, _specs())
    snap = rp.snapshot_events(db)
    at = max(ev.sort_key(e)[0] for e in snap) + 1
    # C1: the snapshot carries the record's stored uuid — an event appended
    # after it references that uuid (rec), not the id
    rec_uuid = db.conn.execute(
        "SELECT uuid FROM records WHERE id='T-000001'").fetchone()[0]
    added = ev.make("item.add", actor="alex", at=at, seq=1, rec=rec_uuid,
                    item=_uuid(), no="003", section="Acceptance Criteria",
                    text="c")
    (tmp_path / "b").mkdir()
    db2 = _db_at(tmp_path / "b" / "db.sqlite")
    rp.replay(db2, [*snap, added], _specs())
    rows = db2.items_load("T-000001")
    assert [r["no"] for r in rows] == ["001", "002", "003"]
    assert {r["no"]: r["text"] for r in rows}["003"] == "c"
    db.close()


def test_snapshot_validates_as_event_lines(tmp_path):
    """A compacted log (snapshot lines) passes events.validate (E7)."""
    mk = _mk_rich()
    (tmp_path / "a").mkdir()
    db = _db_at(tmp_path / "a" / "db.sqlite")
    rp.replay(db, mk, _specs())
    p = tmp_path / "events.jsonl"
    ev.append(p, rp.snapshot_events(db))
    assert ev.validate(p) == []
    db.close()


def test_snapshot_replace_semantics_rerun_is_idempotent(tmp_path):
    """Snapshot events replace state: replaying them twice (via two distinct
    snapshot lines, as a bad re-compact could produce) does not stack."""
    mk = _mk_rich()
    (tmp_path / "a").mkdir()
    db = _db_at(tmp_path / "a" / "db.sqlite")
    rp.replay(db, mk, _specs())
    snap = rp.snapshot_events(db)
    # re-run: at+1 so the lines are distinct (dedup keeps both)
    snap2 = [{**e, "at": e["at"] + 1} for e in snap]
    db2 = _db_at(tmp_path / "a" / "db2.sqlite")
    rp.replay(db2, snap + snap2, _specs())
    assert _norm_tables(db2) == _norm_tables(db)
    db.close()
    db2.close()

# ---------------------------------------------------------------------------
# E8 (P2): randomized determinism — two divergent clones, merged in both
# orders (A+B / B+A), must yield identical tables on every seed.


def _random_clone_events(rng: random.Random, base: list[dict], actor: str):
    """Random divergence on top of a shared prefix: disjoint AND clashing
    creates (id collisions), same-field edits, item add/set/move/delete,
    relations, WIP, project changelog."""
    evs = list(base)
    ut, up = base[0]["rec"], base[1]["rec"]  # the two shared tasks
    for step in range(12):
        n = rng.randint(1, 4)
        for _ in range(n):
            at = evs[-1]["at"] + rng.randint(1, 50)  # later than everything seen
            seq = len([e for e in evs if e["actor"] == actor])
            roll = rng.random()
            if roll < 0.20:
                # create: 1/3 the chance to clash with an existing id
                if rng.random() < 0.33 and evs:
                    src = rng.choice([e for e in evs if e["op"] == "create"])
                    rid, rtype = src["id"], src["type"]
                else:
                    rtype, rid = rng.choice(["task", "decision"]), \
                        rng.choice([f"T-{rng.randint(1, 40):06d}",
                                    f"DEC-{rng.randint(1, 40):04d}"])
                evs.append(ev.make("create", actor=actor, at=at, seq=seq,
                                   rec=_uuid(), type=rtype, id=rid,
                                   title=f"{actor} step", status="Draft",
                                   summary="", sections={}))
            elif roll < 0.45:
                # same-field edits (LWW territory)
                rec = rng.choice([ut, up])
                evs.append(ev.make("set", actor=actor, at=at, seq=seq, rec=rec,
                                   fields={rng.choice(["title", "status"]):
                                           f"by {actor}"}))
            elif roll < 0.75:
                # item add/set/move/delete on one of the shared tasks
                rec = rng.choice([ut, up])
                iu = _uuid()
                no = rng.choice(["001", "002", "003", "A01", "A02"])
                evs.append(ev.make("item.add", actor=actor, at=at, seq=seq,
                                   rec=rec, item=iu, no=no,
                                   section="Acceptance Criteria",
                                   text=f"{actor} item {step}"))
                if rng.random() < 0.6:
                    evs.append(ev.make("item.set", actor=actor,
                                       at=at + 1, seq=seq + 1, rec=rec, item=iu,
                                       fields=rng.choice(
                                           [{"state": "done"},
                                            {"text": f"{actor} edit"},
                                            {"owner": actor},
                                            {"deleted": True},
                                            {"state": "open", "deleted": False}]),
                                       date=f"2026-01-03T0{step % 10}:00:00+00:00"))
            else:
                # relations / WIP / project changelog
                kind = rng.choice(["related", "supersede"])
                others = [e["rec"] for e in evs if e["op"] == "create"]
                evs.append(ev.make("rel.add", actor=actor, at=at, seq=seq,
                                   rec=rng.choice(others), kind=kind,
                                   target=rng.choice(others)))
    return evs


def test_random_determinism_both_merge_orders(seed=0, steps=12):
    """A+B and B+A (union = concat + dedupe, simulated by replay's own
    line-level dedupe) must produce identical tables, for any seed."""
    rng = random.Random(seed)
    specs = _specs()
    ut, up = _uuid(), _uuid()
    base = [_create(ut, "task", "T-000001", "父A", 1_000, "alex"),
            _create(up, "task", "T-000002", "父B", 2_000, "alex")]
    ea = _random_clone_events(random.Random(seed + 1_000_000), base, "alex")
    eb = _random_clone_events(random.Random(seed + 2_000_000), base, "billy")
    tmp = Path(tempfile.mkdtemp(prefix=f"proj-e8-{seed}-"))

    # the columns replay derives from the wall clock (utc_now) when the event
    # does not carry the field - determinism is about the data, not those
    _VOLATILE_TS = ("created", "closed_at", "date", "added_at")

    def tables_of(events):
        (tmp / "db").mkdir(exist_ok=True)
        db = _db_at(tmp / "db" / f"t{rng.random()}.sqlite")
        rp.replay(db, events, specs)
        out = {}
        for t in ("records", "record_changelog", "items", "relations",
                  "wip_entries", "changelog_entries"):
            rows = [dict(r) for r in db.conn.execute(f"SELECT * FROM {t}")]
            for r in rows:
                for k in _VOLATILE_TS:
                    r.pop(k, None)
            out[t] = sorted(json.dumps(r, sort_keys=True) for r in rows)
        return out

    ab = tables_of(ea + eb)
    ba = tables_of(eb + ea)
    if ab != ba:
        for t in ab:
            if ab[t] != ba[t]:
                pytest.fail(f"seed {seed}: table {t} differs between A+B and "
                            f"B+A\nA+B: {ab[t][:3]}...\nB+A: {ba[t][:3]}...")
    assert ab == ba


def test_random_determinism_200_seeds():
    """Run the property over 200 seeds; a failure names the seed (P2)."""
    for seed in range(200):
        test_random_determinism_both_merge_orders(seed)


# deterministic base-seed sanity (CI-friendly, no flakiness by design)
@pytest.mark.parametrize("seed", [1, 7, 42, 123, 999])
def test_random_determinism_named_seeds(seed):
    test_random_determinism_both_merge_orders(seed)
