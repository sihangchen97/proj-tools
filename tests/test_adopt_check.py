"""v3 adoption (pre-existing files) and `proj check` invariants."""
from proj import check as checkmod
from proj.engine import store as rec


def test_adopt_existing_file_generates_uuid(proj):
    d = proj.root / "docs/tech-debt"
    (d / "0001-old-hack.md").write_text(
        "# 0001 Old hack\n\n## Location\nsrc/x.py\n\n## Reason\nlegacy\n",
        encoding="utf-8")
    problems, notes = rec.adopt(proj)
    assert problems == []
    assert any("uuid generated" in n for n in notes)
    text = (d / "0001-old-hack.md").read_text()
    assert text.startswith("<!-- proj:debt ")  # re-rendered: marker line 1, valid-YAML front matter
    assert "\n---\n" in text
    assert "## Location" in text
    # title had its id prefix stripped
    assert "# 1. Old hack" in text
    spec = rec.load_registry(proj.root)["debt"]
    db = rec.open_db(proj)
    assert db.get(spec, "0001", by="id") is not None
    db.close()


def test_adopt_existing_uuid_wins(proj):
    import uuid as _uuid
    u = str(_uuid.uuid4())
    d = proj.root / "docs/decisions"
    (d / "0002-imported.md").write_text(
        f"<!-- proj:decision {u} -->\n\n# 0002 Imported\n", encoding="utf-8")
    problems, _ = rec.adopt(proj)
    assert problems == []
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    assert db.get(spec, u, by="uuid") is not None
    db.close()
    # subsequent sync keeps it
    problems, _ = rec.sync(proj)
    assert problems == []


def test_sync_rejects_unknown_uuid(proj):
    d = proj.root / "docs/decisions"
    (d / "0002-bad.md").write_text(
        f"<!-- proj:decision {'9' * 32} -->\n\n# 0002 Bad\n", encoding="utf-8")
    problems, _ = rec.sync(proj)
    assert any("run `proj adopt`" in p for p in problems)


def test_sync_rejects_id_mismatch(proj):
    d = proj.root / "docs/decisions"
    (d / "9999-wrong.md").write_text(
        "<!-- proj:decision " + "a" * 32 + " -->\n\n# 9999 Wrong\n", encoding="utf-8")
    problems, _ = rec.adopt(proj)
    assert problems == []
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    assert db.all(spec)  # adopted
    db.close()
    # rename the file so id no longer matches
    p = proj.root / "docs/decisions" / "9999-wrong.md"
    p.rename(proj.root / "docs/decisions" / "0003-renamed.md")
    problems, _ = rec.sync(proj)
    assert any("does not match filename" in p for p in problems)


def test_check_clean_project_passes(proj):
    assert checkmod.check(proj) == []


def test_check_detects_db_drift(proj):
    rec.new_record(proj, "decision", "D2")
    # delete the md file → DB has a record with no file
    p = next((proj.root / "docs/decisions").glob("0002-*.md"))
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
    row = db.get(spec, "0002", by="id")
    db.set(spec, row["uuid"], status="Stale")
    db.close()
    problems = checkmod.check(proj)
    assert any("no longer allowed" in p for p in problems)


def test_check_detects_section_drift(proj):
    rec.new_record(proj, "decision", "D2")
    p = next((proj.root / "docs/decisions").glob("0002-*.md"))
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
    p = next((proj.root / "docs/decisions").glob("0002-*.md"))
    p.write_text(p.read_text().replace("## Context\n", "## Context\nunsynced edit\n"))
    assert rec.md_drift(proj) == [p.name]
    # sync clears it
    rec.sync(proj)
    assert rec.md_drift(proj) == []


def test_check_detects_deep_steps(proj):
    row, _ = rec.new_record(proj, "task", "T1")
    p = next((proj.root / "tasks").glob(f"{row['id']}-*.md"))
    t = p.read_text().replace(
        "## Steps\n", "## Steps\n\n- [ ] a\n  - [ ] b\n    - [ ] deep\n")
    p.write_text(t)
    rec.sync(proj)
    problems = checkmod.check(proj)
    assert any("deeper than 2 levels" in p for p in problems)


def test_check_detects_schema_mismatch(proj):
    from proj.core.templates import bundled_dir
    # project override profile with an extra field; DB table stays as built
    tdir = proj.root / "profiles" / "default"
    tdir.mkdir(parents=True)
    yaml = (bundled_dir() / "default" / "decision.yaml").read_text()
    yaml = yaml.replace("  - {name: supersedes",
                        "  - {name: extra}\n  - {name: supersedes")
    (tdir / "decision.yaml").write_text(yaml)
    problems = checkmod.check(proj)
    assert any("does not match" in p for p in problems)
    # remove the override — check passes again
    (tdir / "decision.yaml").unlink()
    tdir.rmdir()
    assert not any("does not match" in p for p in checkmod.check(proj))
