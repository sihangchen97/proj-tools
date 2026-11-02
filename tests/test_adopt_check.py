"""v3 adoption (pre-existing files) and `proj check` invariants."""
from proj import check as checkmod
from proj import scaffold
from proj.engine import store as rec


def test_adopt_existing_file(proj):
    d = proj.root / "docs/debts"
    (d / "DEB-0001-old-hack.md").write_text(
        "# Old hack\n\n## Location\nsrc/x.py\n\n## Reason\nlegacy\n",
        encoding="utf-8")
    problems, notes = rec.adopt(proj)
    assert problems == []
    assert any("adopted as debt DEB-0001" in n for n in notes)
    text = (d / "DEB-0001-old-hack.md").read_text()
    assert text.startswith("<!-- proj:debt DEB-0001 -->")  # id marker comment on line 1
    assert "> **status:**" in text  # blockquote head
    assert "## Location" in text
    assert "# DEB-0001. Old hack" in text  # the re-rendered H1 carries the id
    spec = rec.load_registry(proj.root)["debt"]
    db = rec.open_db(proj)
    assert db.get(spec, "DEB-0001") is not None
    db.close()


def test_adopt_existing_id_marker_wins(proj):
    rec.new_record(proj, "decision", "Imported")  # DEC-0001
    d = proj.root / "docs/decisions"
    # a second pre-existing file carrying a valid id marker for a fresh id
    (d / "DEC-0002-imported.md").write_text(
        "<!-- proj:decision dec-0002 -->\n\n# Imported two\n", encoding="utf-8")
    problems, _ = rec.adopt(proj)
    assert problems == []
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    assert db.get(spec, "DEC-0002") is not None  # matched case-insensitively, uppercased
    db.close()
    # subsequent sync keeps it
    problems, _ = rec.sync(proj)
    assert problems == []


def test_sync_rejects_unknown_id(proj):
    d = proj.root / "docs/decisions"
    # a well-formed id marker for a record that was never created
    (d / "DEC-0099-bad.md").write_text(
        "<!-- proj:decision DEC-0099 -->\n\n# Bad\n", encoding="utf-8")
    problems, _ = rec.sync(proj)
    assert any("run `proj adopt`" in p for p in problems)


def test_sync_rejects_id_mismatch(proj):
    d = proj.root / "docs/decisions"
    (d / "DEC-0001-wrong.md").write_text(
        "<!-- proj:decision DEC-0001 -->\n\n# Wrong\n", encoding="utf-8")
    problems, _ = rec.adopt(proj)
    assert problems == []
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    assert db.all(spec)  # adopted
    db.close()
    # rename the file so its prefix no longer matches the marker id
    p = proj.root / "docs/decisions" / "DEC-0001-wrong.md"
    p.rename(proj.root / "docs/decisions" / "DEC-0099-renamed.md")
    problems, _ = rec.sync(proj)
    assert any("does not match" in p for p in problems)


def test_check_clean_project_passes(proj):
    assert checkmod.check(proj) == []


def test_check_detects_db_drift(proj):
    rec.new_record(proj, "decision", "D2")
    # delete the md file → DB has a record with no file
    p = next((proj.root / "docs/decisions").glob("DEC-0002-*.md"))
    p.unlink()
    problems = checkmod.check(proj)
    assert any("has no md file" in p for p in problems)


def test_check_detects_orphans_when_dir_missing(proj):
    # whole record dir removed → DB rows are still flagged (dir gap must not skip)
    rec.new_record(proj, "incident", "DB down")
    d = proj.root / "docs/incidents"
    for f in d.glob("*.md"):
        f.unlink()
    d.rmdir()
    problems = checkmod.check(proj)
    assert any("has no md file" in p for p in problems)


def test_check_detects_status_not_in_template(proj):
    # DB row whose status the template no longer allows (status rename scenario)
    rec.new_record(proj, "decision", "D2")
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    row = db.get(spec, "DEC-0002")
    db.set(spec, row["id"], status="Stale")
    db.close()
    problems = checkmod.check(proj)
    assert any("no longer allowed" in p for p in problems)


def test_check_detects_section_drift(proj):
    rec.new_record(proj, "decision", "D2")
    p = next((proj.root / "docs/decisions").glob("DEC-0002-*.md"))
    text = p.read_text().replace("## Context\n", "## Context\nuser edit here\n")
    p.write_text(text)
    rec.sync(proj)  # DB now holds the edit
    # drift it again without sync
    p.write_text(p.read_text().replace("user edit here", "drifted\n"))
    problems = checkmod.check(proj)
    assert any("differs from DB" in p for p in problems)


def test_md_drift_detects_unsynced_edit(proj):
    assert rec.md_drift(proj) == []
    rec.new_record(proj, "decision", "D2")
    p = next((proj.root / "docs/decisions").glob("DEC-0002-*.md"))
    p.write_text(p.read_text().replace("## Context\n", "## Context\nunsynced edit\n"))
    assert rec.md_drift(proj) == [p.name]
    # sync clears it
    rec.sync(proj)
    assert rec.md_drift(proj) == []


def test_check_detects_schema_mismatch(proj):
    from proj.core import db as dbmod
    # drop a generic column from the shared records table → template mismatch
    d = dbmod.Db(proj.db_path)
    d.conn.execute("DROP TABLE records")
    d.conn.execute(
        "CREATE TABLE records (id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, "
        "status TEXT, created TEXT, sections TEXT)")  # summary missing
    d.close()
    problems = checkmod.check(proj)
    assert any("does not match" in p for p in problems)
    # dev phase: no migration — rebuild the DB file (re-init re-adopts the md)
    proj.db_path.unlink()
    scaffold.init(proj, ["default"])
    assert not any("does not match" in p for p in checkmod.check(proj))


def test_check_ignores_missing_events_log(proj):
    """No events.jsonl yet (pre-S.10 recording): check stays healthy about it."""
    assert not any("events.jsonl" in p for p in checkmod.check(proj))


def test_check_validates_events_log(proj):
    path = proj.root / "events.jsonl"
    path.write_text("not a json line\n", encoding="utf-8")
    problems = checkmod.check(proj)
    assert any("events.jsonl" in p and "line 1" in p for p in problems)
    path.unlink()


def test_check_accepts_well_formed_events_log(proj):
    from proj import events
    path = proj.root / "events.jsonl"
    ev = events.make("log", actor="alice", at=1, seq=1, rec="T-000001",
                     category="Added", text="created")
    path.write_text(events.to_line(ev), encoding="utf-8")
    assert not any("events.jsonl" in p for p in checkmod.check(proj))
    path.unlink()
