"""v3 global CHANGELOG.md from the DB."""
import pytest

from proj import changelog as cl
from proj import db as dbmod
from proj import records as rec
from proj.views import bump


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
    # date-numbered kinds work through the same engine
    row, _ = rec.new_record(proj, "incident", "DB down")
    assert row["id"].startswith("20")  # date-based id
    row2, _ = rec.new_record(proj, "experiment", "lr sweep")
    assert row2["id"] == "0001"  # experiments are 4-digit (design table)
    # field round-trips
    spec = rec.load_registry(proj.root)["experiment"]
    db = rec.open_db(proj)
    r = db.get(spec, row2["id"], by="id")
    db.set(spec, r["uuid"], commit="abc123")
    r = db.get(spec, row2["id"], by="id")
    assert r["commit"] == "abc123"
    db.close()
