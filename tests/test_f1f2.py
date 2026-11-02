"""F1/F2: file fingerprints (plans/hard-parts §F1/§F2 acceptance).

- a write stores the md's (mtime_ns, size, sha1) fingerprint (records) and the
  rendered views' (meta `fp:<name>`);
- ensure_synced is stat-first: an unchanged file is skipped without a read; a
  pure mtime change (touch) is NOT ingested (no new changelog row);
- render_all does not read back: an unchanged file is skipped on hash+stat;
- `proj check` (md_drift) still reads the full content — the fingerprint is
  cache, not the source of truth.
"""
from __future__ import annotations

import os

import pytest

from proj.engine import store as rec

SECTION = "## Decision"  # a decision md's text section (profiles/default)


def test_fingerprint_stored_after_new(proj):
    """F1: creating a record writes its md and stores (mtime, size, sha1), and
    the rendered global views are fingerprinted in meta."""
    row, p = rec.new_record(proj, "decision", "A decision")
    with rec.open_db(proj) as db:
        fp = db.fingerprint_of(row["id"])
    assert fp is not None
    assert p.exists()
    assert (fp[0], fp[1]) == (p.stat().st_mtime_ns, p.stat().st_size)
    with rec.open_db(proj) as db:
        assert db.view_fp("WIP.md") is not None  # WIP.md always renders
        assert db.view_fp("CHANGELOG.md") is not None


def test_unchanged_sync_and_render_are_noops(proj):
    """F1/F2: nothing hand-edited → sync ingests nothing and render_all skips
    every record file (the stored fingerprints match) — only the global views
    may be written (a fresh render)."""
    for i in range(10):
        rec.new_record(proj, "decision", f"Decision {i}")
    rec.ensure_synced(proj)  # no drift → nothing ingested
    with rec.open_db(proj) as db:
        written = rec.render_all(proj, db)
    assert all(p.name in ("WIP.md", "CHANGELOG.md") for p in written)


def test_hand_edit_ingests_once(proj):
    """F1: one hand-edited record → its md is ingested (md wins, the hand line
    is in the DB) and the fingerprint refreshed; then the file is re-rendered
    (tool-owned parts win) so the DB and the file match again."""
    row, p = rec.new_record(proj, "decision", "Decision X")
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace(SECTION, SECTION + "\n\nhand note 123"),
                 encoding="utf-8")
    rec.sync(proj)
    with rec.open_db(proj) as db:
        fp = db.fingerprint_of(row["id"])
    assert fp is not None and (fp[0], fp[1]) == (p.stat().st_mtime_ns,
                                                 p.stat().st_size)
    # the re-rendered file now matches the DB (a fresh sync reads nothing)
    assert p.name not in rec.md_drift(proj)


def test_touch_is_not_ingested(proj):
    """F1.3: a pure mtime change (touch) — same content, moved mtime: the
    fingerprint's mtime changes (the file is re-stat'd), but NO ingest
    (no new changelog row)."""
    row, p = rec.new_record(proj, "decision", "Decision T")
    with rec.open_db(proj) as db:
        before = len(db.logs(row["id"]))
        fp0 = db.fingerprint_of(row["id"])
    new_mtime = fp0[0] + 10**9  # +1 s, content untouched
    os.utime(p, ns=(new_mtime, new_mtime))
    rec.sync(proj)
    with rec.open_db(proj) as db:
        assert len(db.logs(row["id"])) == before  # NOT ingested
        assert db.fingerprint_of(row["id"])[0] != fp0[0]  # fp re-stat'd


@pytest.mark.render_drift
def test_md_drift_still_reads_content(proj):
    """F1.3: the fingerprint is cache — `proj check` (md_drift) reads the full
    content, so a hand edit is still caught even before any fingerprint would
    say 'unchanged'."""
    _row, p = rec.new_record(proj, "decision", "Decision C")
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace(SECTION, SECTION + "\n\nsecret edit"),
                 encoding="utf-8")
    assert p.name in rec.md_drift(proj)  # full-content compare finds the edit
