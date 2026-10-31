"""v3 global CHANGELOG.md from the DB."""
import pytest

from proj.core import db as dbmod
from proj.engine import store as rec
from proj.engine.views import bump
from proj.record_types import changelog as cl


def test_parse_summary():
    assert dbmod.parse_summary("Added: new thing") == ("Added", "new thing")
    assert dbmod.parse_summary("added: lowercase ok") == ("Added", "lowercase ok")
    assert dbmod.parse_summary("plain text") == ("Changed", "plain text")
    assert dbmod.parse_summary("Fixed: off by one") == ("Fixed", "off by one")
    with pytest.raises(ValueError, match="unknown changelog category"):
        dbmod.parse_summary("Wrong: nope")


def test_add_and_release(proj):
    cl.add(proj, "Added: sqlite core")
    cl.add(proj, "Changed: CLI rewrite")
    text = (proj.root / "CHANGELOG.md").read_text()
    assert "## [Unreleased]" in text
    assert "- sqlite core" in text
    assert "- CLI rewrite" in text

    used = cl.version_bump(proj, None, "minor")
    assert used == "v0.1.0"
    text = (proj.root / "CHANGELOG.md").read_text()
    assert "## [v0.1.0] - " in text
    assert "Unreleased]" in text  # still has the (now empty) section
    assert "sqlite core" in text


def test_bump_base_is_highest_version_not_newest_date(proj):
    # v1.0.0 released first, then an older v0.9.9 backfilled (newer date)
    cl.add(proj, "Added: base")
    cl.version_bump(proj, "v1.0.0", None)
    cl.add(proj, "Added: backfill")
    cl.version_bump(proj, "v0.9.9", None)
    cl.add(proj, "Added: next")
    used = cl.version_bump(proj, None, "minor")
    assert used == "v1.1.0"  # not v0.10.0 (newest-date base)


def test_read_versions_numeric_order(proj):
    # 'v1.10.0' must sort above 'v1.9.0' (semver), not string order
    from proj.engine import views
    db = rec.open_db(proj)
    db.add_cl_entry("v1.9.0", "Added", "x", "2026-01-01")
    db.add_cl_entry("v1.10.0", "Added", "y", "2026-01-02")
    versions = [v for v, _ in views.read_versions(proj, db)]
    db.close()
    assert versions[0] == "v1.10.0" and versions[1] == "v1.9.0"


def test_release_notes(proj):
    cl.add(proj, "Added: x")
    cl.add(proj, "Fixed: y")
    cl.version_bump(proj, "v1.2.3", None)
    notes = cl.release_notes(proj, "v1.2.3")
    assert "### Added" in notes
    assert "### Fixed" in notes
    with pytest.raises(ValueError):
        cl.release_notes(proj, "v9.9.9")


def test_bump_math():
    assert bump("0.0.9", "patch") == "0.0.10"
    assert bump("0.1.9", "minor") == "0.2.0"
    assert bump("1.2.3", "major") == "2.0.0"


def test_show_versions(proj):
    cl.add(proj, "Added: a")
    cl.version_bump(proj, "v0.1.0", None)
    cl.add(proj, "Added: b")
    cl.version_bump(proj, "v0.2.0", None)
    versions = cl.show_versions(proj)
    assert [v for v, _ in versions] == ["v0.2.0", "v0.1.0"]


def test_incident_and_experiment_kinds(proj):
    # all record_types are digit-numbered (int > 0, D9) through the same engine
    row, _ = rec.new_record(proj, "incident", "DB down")
    assert row["id"] == "INC-0001"  # incident is code + 4-digit
    row2, _ = rec.new_record(proj, "experiment", "lr sweep")
    assert row2["id"] == "EXP-0001"  # experiments are code + 4-digit (design table)
    # field round-trips
    spec = rec.load_registry(proj.root)["experiment"]
    db = rec.open_db(proj)
    r = db.get(spec, row2["id"])
    db.set(spec, r["id"], commit="abc123")
    r = db.get(spec, row2["id"])
    assert r["commit"] == "abc123"
    db.close()
