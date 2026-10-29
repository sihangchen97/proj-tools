"""v3 decision engine: new, supersede, status, toc/graph."""
import pytest

from proj import decision as dec
from proj import records as rec


def test_new_creates_record(proj):
    row, p = rec.new_record(proj, "decision", "Use Rust")
    assert row["id"] == "0002"
    assert row["status"] == "Proposed"
    text = p.read_text()
    assert text.startswith(f"<!-- proj:decision {row['uuid']} -->")
    assert "# 2. Use Rust" in text
    assert "Status: Proposed" in text
    assert "## Context" in text
    # changelog shows the created entry
    assert "| Added | created |" in text


def test_supersede_updates_both(proj):
    row, _ = dec.new(proj, "New decision", supersede="1")
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    old = db.get(spec, row["supersedes"], by="id")
    new = db.get(spec, row["id"], by="id")
    db.close()
    assert old["status"] == "Superseded by DEC-0002"
    assert new["status"] == "Supersedes DEC-0001"
    assert new["supersedes"] == "0001"


def test_status_validates_allowed_set(proj):
    rec.new_record(proj, "decision", "D2")
    rec.status_set(proj, "decision", "2", "Accepted")
    with pytest.raises(ValueError, match="not allowed"):
        rec.status_set(proj, "decision", "2", "Bogus")


def test_status_supersede_pattern(proj):
    # "Supersedes DEC-0001" only matches when id==0001
    with pytest.raises(ValueError, match="not allowed"):
        rec.status_set(proj, "decision", "1", "Supersedes DEC-0002")
    rec.status_set(proj, "decision", "1", "Supersedes DEC-0001")
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    assert db.get(spec, "0001", by="id")["status"] == "Supersedes DEC-0001"
    db.close()


def test_status_change_auto_logs(proj):
    row, _ = rec.new_record(proj, "decision", "D2")  # starts Proposed
    rec.status_set(proj, "decision", row["id"], "Accepted")
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    r = db.get(spec, row["id"], by="id")
    logs = db.logs("decision", r["uuid"])
    db.close()
    assert any("Proposed → Accepted" in e["summary"] for e in logs)


def test_toc_and_graph(proj):
    dec.new(proj, "Second", supersede="1")
    g = dec.graph(proj)
    assert "graph LR" in g
    assert "D0001 --> D0002" in g
    t = dec.toc(proj)
    assert "Superseded by DEC-0002" in t


def test_link_appends_to_consequences(proj):
    dec.new(proj, "Third")
    dec.link(proj, "1", ["3"])
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    row = db.get(spec, "0001", by="id")
    db.close()
    import json
    assert "See also: DEC-3" in json.loads(row["sections"])["consequences"]
