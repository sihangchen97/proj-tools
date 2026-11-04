"""Regressions for the 2026-11-05 code review (plans/CODE_REVIEW-2026-11-05.md,
part 1): incremental replay stays read-side (#1/#3), the release event carries
the project's unreleased placeholder (#5), C5 sees worktrees and monorepo
subdirs (#6), store.add_log honors A1 (#7), next_id off-by-one (#8), the
delete event's uuid (#9), set() validation first (#10a)."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from proj import events as ev
from proj import hooks
from proj import replay as rp
from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.core.templates import changelog_path, load_registry
from proj.engine import store as rec


def _specs() -> dict:
    return {s.record_type: s for s in load_registry(None).values()}


def _task_spec():
    return next(s for s in load_registry(None).values() if s.record_type == "task")


def _create(uuid, id_, title, at, actor):
    return ev.make("create", actor=actor, at=at, seq=1, rec=uuid,
                   type="task", id=id_, title=title, status="Draft",
                   summary="", sections={})


# ---------------------------------------------------------------------------
# #1: incremental replay is read-side — the catch-up (the bare-Db path every
# READ command takes) must not re-append the events it just read.


def test_apply_incremental_does_not_rewrite_the_log(tmp_path):
    """The plan's reproduction in-process: the cache is behind, an external
    writer appended events, a read command catches up — events.jsonl must be
    byte-identical before and after (before the fix db.close() re-appended
    the just-read create under a fresh at/seq, permanently polluting the log)."""
    p = tmp_path / "events.jsonl"
    u1, u2 = "u" + "0" * 31, "v" + "0" * 31
    ev.append(p, [_create(u1, "T-000001", "a", 1_000, "alex")])
    (tmp_path / "a").mkdir()
    db = dbmod.Db(tmp_path / "a" / "cache.sqlite")
    rp.replay(db, [_create(u1, "T-000001", "a", 1_000, "alex")], _specs())
    rp.commit_meta(db, p)
    ev.append(p, [_create(u2, "T-000002", "b", 2_000, "billy")])
    before = p.read_bytes()
    # the bare Db a READ command opens (ensure_cache's append path): emitting
    # is True by default — the fix flips it OFF inside apply_incremental
    kind, lines = rp.plan_incremental(p, rp.load_meta(db))
    assert kind == "append"
    res = rp.apply_incremental(db, lines, _specs())
    rp.apply_and_store(db, res, p)
    assert not db._events  # the fix: nothing accumulated, nothing to flush
    db.close()
    assert p.read_bytes() == before  # no re-append
    db2 = dbmod.Db(tmp_path / "a" / "cache.sqlite")
    assert db2.conn.execute("SELECT count(*) FROM records").fetchone()[0] == 2
    db2.close()  # the catch-up itself worked


def test_read_command_after_external_append_keeps_the_log_stable(proj):
    """End-to-end via the store: two write commands, the cache rolled back to
    after the first, a READ (open_db → ensure_cache) catches up — the log must
    not grow (before the fix it gained a duplicate create of T-000002, and
    every later full rebuild then failed with an IntegrityError)."""
    cfg = proj
    rec.new_record(cfg, "task", "first")
    # checkpoint + back up the cache (WAL: force data into the main file first)
    conn = dbmod.Db(cfg.db_path)
    conn.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    backup = cfg.db_path.read_bytes()
    rec.new_record(cfg, "task", "second")
    cfg.db_path.write_bytes(backup)  # the cache now lags one event behind
    for suffix in ("-wal", "-shm"):
        (cfg.db_path.parent / (cfg.db_path.name + suffix)).unlink(missing_ok=True)
    log = cfg.root / ".proj" / "events.jsonl"
    before = log.read_bytes()
    rec.open_db(cfg)  # a READ — ensure_cache's append path must not write the log
    assert log.read_bytes() == before
    db = dbmod.Db(cfg.db_path)
    rows = [r[0] for r in db.conn.execute(
        "SELECT id FROM records WHERE type='task' ORDER BY id")]
    db.close()
    assert rows == ["T-000001", "T-000002"]  # catch-up applied, no dupes


# ---------------------------------------------------------------------------
# #3: _flush_events must not advance ev:size past events an EXTERNAL writer
# appended while this command ran — the next command repairs them instead of
# skipping them forever.


def test_flush_does_not_skip_external_events(tmp_path):
    p = tmp_path / "events.jsonl"
    u1, u2 = "u" + "0" * 31, "v" + "0" * 31
    ev.append(p, [_create(u1, "T-000001", "a", 1_000, "alex")])
    (tmp_path / "a").mkdir()
    db = dbmod.Db(tmp_path / "a" / "cache.sqlite")
    rp.replay(db, ev.read(p), _specs())
    rp.commit_meta(db, p)  # the cache now covers the whole file
    size_after_first = p.stat().st_size
    db.close()
    # an external writer appends a line the cache never applied (the failure
    # the review describes: a swallowed incremental, a short-circuited rebuild)
    ev.append(p, [_create(u2, "T-000002", "b", 2_000, "billy")])
    # the next LOCAL write command: its flush must NOT stamp ev:size past the
    # external line — keep the old value so the following command replays it
    db2 = dbmod.Db(tmp_path / "a" / "cache.sqlite")
    db2.add_log("T-000001", "note")
    db2.close()
    db3 = dbmod.Db(tmp_path / "a" / "cache.sqlite")
    got_size = db3.conn.execute(
        "SELECT value FROM meta WHERE key='ev:size'").fetchone()[0]
    db3.close()
    assert int(got_size) == size_after_first  # NOT advanced past the external line
    # the following command therefore does NOT skip the external line: it takes
    # one of the repair paths (a 'noop' would have silently dropped it — the
    # pre-fix behavior; 'full' is legitimate when the external line's sort key
    # sorts before the local one, a 'append' when it does not)
    db4 = dbmod.Db(tmp_path / "a" / "cache.sqlite")
    kind, _ = rp.plan_incremental(p, rp.load_meta(db4))
    db4.close()
    assert kind in ("append", "full")
    # ... and a full rebuild lands the external line in the cache
    with tempfile.TemporaryDirectory() as td:
        fresh = dbmod.Db(Path(td) / "r.sqlite")
        rp.replay(fresh, ev.read(p), _specs())
        assert fresh.conn.execute(
            "SELECT count(*) FROM records").fetchone()[0] == 2
        fresh.close()


# ---------------------------------------------------------------------------
# #5: the changelog.release event carries the project's unreleased placeholder
# (profiles/changelog.yaml `unreleased` — a project may rename it); the
# replay stamps the same rows instead of a hard-coded "Unreleased".


def test_release_event_field_and_replay(tmp_path):
    db = dbmod.Db(tmp_path / "a.sqlite")
    db.add_cl_entry("WIP", "Added", "x")
    db.release_cl("v0.1.0", "WIP")
    assert db._events and db._events[-1]["op"] == "changelog.release"
    assert db._events[-1]["unreleased"] == "WIP"
    db.close()
    # replay: a fresh db must land the same state — the UPDATE matches the
    # project's placeholder, not the constant
    line = ev.to_line({"op": "changelog.release", "rec": "", "actor": "t",
                       "at": 1, "seq": 1, "version": "v0.1.0", "unreleased": "WIP"})
    (tmp_path / "b").mkdir()
    db2 = dbmod.Db(tmp_path / "b" / "c.sqlite")
    db2.emitting = False
    db2.add_cl_entry("WIP", "Added", "x")
    rp._apply(rp._State(_specs()), db2, ev.from_line(line))
    versions = [r[0] for r in db2.conn.execute(
        "SELECT DISTINCT version FROM changelog_entries")]
    assert versions == ["v0.1.0"]
    db2.close()
    # legacy event (no field) still replays against the constant
    line_old = ev.to_line({"op": "changelog.release", "rec": "", "actor": "t",
                          "at": 1, "seq": 1, "version": "v0.1.0"})
    (tmp_path / "c").mkdir()
    db3 = dbmod.Db(tmp_path / "c" / "c.sqlite")
    db3.emitting = False
    db3.add_cl_entry("Unreleased", "Added", "x")
    rp._apply(rp._State(_specs()), db3, ev.from_line(line_old))
    versions = [r[0] for r in db3.conn.execute(
        "SELECT DISTINCT version FROM changelog_entries")]
    assert versions == ["v0.1.0"]
    db3.close()


@pytest.mark.render_drift
def test_release_roundtrip_with_renamed_placeholder(proj):
    """A project that renames the placeholder: add + release, then a FULL
    rebuild from the log must reproduce the cache (the pre-fix rebuild left
    the entries under 'Unreleased' and `proj check` would flag the drift)."""
    cfg = proj
    (cfg.root / "profiles").mkdir(exist_ok=True)
    bundled = cfg.root / "profiles" / "changelog.yaml"
    bundled.write_text(changelog_path(None).read_text(encoding="utf-8")
                       .replace("unreleased: Unreleased", "unreleased: WIP"),
                       encoding="utf-8")
    from proj.record_types import changelog as cl
    cl.add(cfg, "Added: renamed placeholder")
    cl.version_bump(cfg, "v0.1.0", None)
    db = dbmod.Db(cfg.db_path)
    entries = {e["version"] for e in db.cl_entries()}
    db.close()
    assert entries == {"v0.1.0"}
    # full rebuild from the log: same state
    with tempfile.TemporaryDirectory() as td:
        fresh = dbmod.Db(Path(td) / "rebuild.sqlite")
        rp.replay(fresh, ev.read(cfg.root / ".proj" / "events.jsonl"),
                  load_registry(cfg.root))
        assert {e["version"] for e in fresh.cl_entries()} == entries
        fresh.close()
    # (the on-disk CHANGELOG.md still shows the WIP heading — render_drift)


# ---------------------------------------------------------------------------
# #6: C5 sees a merge in flight from a git WORKTREE (.git is a FILE there)
# and from a MONOREPO subdirectory (no .git at the project root).


def _git(root, *args) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    p = subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True, text=True, env=env)
    return p.stdout.strip()


def test_git_in_progress_in_a_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "f.txt").write_text("x")
    _git(main, "add", ".")
    _git(main, "commit", "-q", "-m", "base")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt))
    cfg = ProjConfig(root=wt)
    assert hooks.git_in_progress(cfg) is None  # idle worktree: still allowed
    # a merge in flight: MERGE_HEAD lives in the REAL git dir, not in the
    # worktree's .git FILE (the pre-fix .git-dir check saw nothing)
    real_dir = (wt / _git(wt, "rev-parse", "--git-dir")).resolve()
    (real_dir / "MERGE_HEAD").write_text("0" * 40)
    try:
        assert hooks.git_in_progress(cfg) is not None
    finally:
        (real_dir / "MERGE_HEAD").unlink()


def test_git_in_progress_in_a_monorepo_subdir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    sub = repo / "sub"  # the project root — NO .git dir of its own
    sub.mkdir()
    cfg = ProjConfig(root=sub)
    assert hooks.git_in_progress(cfg) is None
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40)
    try:
        assert hooks.git_in_progress(cfg) is not None
    finally:
        (repo / ".git" / "MERGE_HEAD").unlink()


def test_git_in_progress_still_sees_a_plain_merge(proj):
    """The pre-existing case (a project root that IS a checkout) is unchanged."""
    _git(proj.root, "init", "-q")
    assert hooks.git_in_progress(proj) is None
    (proj.root / ".git" / "MERGE_HEAD").write_text("0" * 40)
    try:
        assert hooks.git_in_progress(proj) is not None
    finally:
        (proj.root / ".git" / "MERGE_HEAD").unlink()


# ---------------------------------------------------------------------------
# #7: store.add_log honors A1 (an unsynced hand edit is ingested, never
# clobbered) and refreshes the views through render_affected.


def test_add_log_does_not_clobber_unsynced_hand_edit(proj):
    row, p = rec.new_record(proj, "task", "manual log")
    # an unsynced hand edit: the user appends free text to a user-owned section
    md = p.read_text(encoding="utf-8")
    marker = "<!-- proj:section — USER-OWNED"
    i = md.index(marker)
    j = md.index("\n", i)  # the marker line's end
    p.write_text(md[:j] + "\n\n- the user's own note\n" + md[j:], encoding="utf-8")
    rec.add_log(proj, "task", row["id"], "a log line")
    out = p.read_text(encoding="utf-8")
    assert "- the user's own note" in out  # ingested, not clobbered
    assert "a log line" in out


def test_add_log_keeps_views_intact(proj):
    row, _ = rec.new_record(proj, "task", "views")
    rec.status_set(proj, "task", row["id"], "In Progress")  # the task enters WIP.md
    wip_before = (proj.root / "WIP.md").read_text(encoding="utf-8")
    rec.add_log(proj, "task", row["id"], "progress note")
    assert (proj.root / "WIP.md").read_text(encoding="utf-8") == wip_before
    spec = rec.spec_of(proj, "task")
    fresh = dbmod.Db(proj.db_path).get(spec, row["id"])
    md = rec.file_path(proj, spec, fresh).read_text(encoding="utf-8")
    assert "progress note" in md


# ---------------------------------------------------------------------------
# #8: next_id off-by-one — the (w+1)th value must raise, not mint a
# (w+1)-digit id that no longer matches the id shape (the md marker stops
# parsing it).


def test_next_id_raises_at_the_width_boundary(tmp_path):
    spec = next(s for s in load_registry(None).values() if s.numbered == 4)
    db = dbmod.Db(tmp_path / "ids.sqlite")
    try:
        db.insert(spec, f"{spec.code}-9999", "last", "Draft", dbmod.utc_now())
        with pytest.raises(ValueError, match="exhausted"):
            dbmod.next_id(spec, db)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# #9: the delete event carries the record's uuid (a pre-fix event had rec=""
# and its replay reported "unknown record uuid").


def test_delete_event_carries_the_record_uuid(tmp_path):
    spec = _task_spec()
    db = dbmod.Db(tmp_path / "del.sqlite")
    try:
        uuid = db.insert(spec, "T-000001", "x", "Draft", dbmod.utc_now())
        db.delete(spec, "T-000001")
        evs = [e for e in db._events if e["op"] == "delete"]
        assert evs and evs[0]["rec"] == uuid
    finally:
        db.close()


def test_delete_replay_no_unknown_record(tmp_path):
    """create + delete (uuid carried) replay cleanly and leave no rows."""
    u = "u" + "0" * 31
    mk = [_create(u, "T-000001", "x", 1_000, "alex"),
          ev.make("delete", actor="alex", at=2_000, seq=1, rec=u)]
    db = dbmod.Db(tmp_path / "dr.sqlite")
    res = rp.replay(db, mk, _specs())
    assert res.diagnostics == []
    assert db.conn.execute("SELECT count(*) FROM records").fetchone()[0] == 0
    db.close()


# ---------------------------------------------------------------------------
# #10a: set() validates the column whitelist BEFORE emitting any event (a bad
# name raises with an empty event buffer, not an emitted-then-rolled-back one).


def test_set_rejects_unknown_column_before_emit(tmp_path):
    spec = _task_spec()
    db = dbmod.Db(tmp_path / "set.sqlite")
    try:
        db.insert(spec, "T-000001", "x", "Draft", dbmod.utc_now())
        db._events.clear()
        with pytest.raises(ValueError, match="unknown record column"):
            db.set(spec, "T-000001", bogus_column="v")
        assert db._events == []  # nothing emitted before the check
    finally:
        db.close()
