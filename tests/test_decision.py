"""v3 decision engine: new, supersede, status, toc/graph."""
import pytest

from proj.engine import store as rec
from proj.record_types import decision as dec


def test_new_creates_record(proj):
    row, p = rec.new_record(proj, "decision", "Use Rust")
    assert row["id"] == "0002"
    assert row["status"] == "Proposed"
    text = p.read_text()
    lines = text.splitlines()
    assert lines[0] == f"<!-- proj:decision {row['uuid']} -->"  # uuid marker comment, line 1
    assert lines[1] == "# 2. Use Rust"  # title below the marker
    assert "> **status:** 🌱 Proposed  " in lines  # blockquote below the title
    assert "# 2. Use Rust" in text
    assert "## Context" in text
    # changelog shows the created entry
    assert "| Added | created |" in text


def test_supersede_sets_fields_not_statuses(proj):
    row, _ = dec.new(proj, "New decision", supersede=["1"])
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    old = db.get(spec, row["supersedes"], by="id")
    new = db.get(spec, row["id"], by="id")
    db.close()
    # supersede is a relationship (fields); statuses stay untouched
    assert old["superseded_by"] == "0002"
    assert new["supersedes"] == "0001"
    assert old["status"] == "Accepted"  # seeded 0001 starts Accepted
    assert new["status"] == "Proposed"


def test_status_validates_allowed_set(proj):
    rec.new_record(proj, "decision", "D2")
    rec.status_set(proj, "decision", "2", "Accepted")
    with pytest.raises(ValueError, match="not allowed"):
        rec.status_set(proj, "decision", "2", "Bogus")


def test_status_change_auto_logs(proj):
    row, _ = rec.new_record(proj, "decision", "D2")  # starts Proposed
    rec.status_set(proj, "decision", row["id"], "Accepted")
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    r = db.get(spec, row["id"], by="id")
    logs = db.logs("decision", r["uuid"])
    db.close()
    assert any("Proposed → Accepted" in e["summary"] for e in logs)


def test_supersede_multi_and_append(proj):
    dec.new(proj, "B")  # 0002
    dec.new(proj, "C")  # 0003
    # one new decision replaces several old ones
    row, _ = dec.new(proj, "Consolidate", supersede=["1", "3"])  # 0004
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    new = db.get(spec, row["id"], by="id")
    d1 = db.get(spec, "0001", by="id")
    db.close()
    assert new["supersedes"] == "0001, 0003"
    assert d1["superseded_by"] == "0004"
    # a later decision appending to the same old one keeps both refs
    dec.new(proj, "Even newer", supersede=["1"])  # 0005
    db = rec.open_db(proj)
    d1 = db.get(spec, "0001", by="id")
    db.close()
    assert d1["superseded_by"] == "0004, 0005"


def test_check_flags_dangling_and_cycle(proj):
    from proj import check as checkmod
    dec.new(proj, "B")  # 0002
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    # dangling forward ref
    row = db.get(spec, "0002", by="id")
    db.set(spec, row["uuid"], supersedes="0099")
    problems = checkmod.check(proj)
    assert any("supersedes 0099" in p for p in problems)
    # manual cycle: 0002 → 0001 and 0001 → 0002
    d1 = db.get(spec, "0001", by="id")
    db.set(spec, row["uuid"], supersedes="0001")
    db.set(spec, d1["uuid"], supersedes="0002")
    db.close()
    problems = checkmod.check(proj)
    assert any("cycle" in p for p in problems)
    assert any("superseded_by is missing" in p for p in problems)


def test_toc_and_graph(proj):
    dec.new(proj, "Second", supersede=["1"])
    g = dec.graph(proj)
    assert "graph LR" in g
    assert "D0001 --> D0002" in g
    t = dec.toc(proj)
    # table format; Status column shows emoji only (emojify); supersede is a field
    assert "| ID | Status | Title |" in t
    assert "| 0002 | 🌱 | Second |" in t
    assert "Superseded" not in t and "Supersedes" not in t
    # one-line legend below the record table (table views only): emoji + name
    assert "* 🌱 Proposed · ✅ Accepted · 📛 Deprecated" in t
    # emojify compares slug + lowercase: case/space variants still map
    from proj.core.templates import load_registry
    spec = load_registry(proj.root)["decision"]
    assert spec.emojify("accepted") == spec.emojify("Accepted") == "✅"
    assert spec.emojify("proposed") == "🌱"


def test_link_appends_to_consequences(proj):
    dec.new(proj, "Third")
    dec.link(proj, "1", ["3"])
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    row = db.get(spec, "0001", by="id")
    db.close()
    import json
    assert "See also: DEC-3" in json.loads(row["sections"])["consequences"]
