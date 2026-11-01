"""v3 decision engine: new, supersede, status, toc/graph."""
import pytest

from proj.engine import store as rec
from proj.engine.record import record_for


def dec_obj(proj):
    return record_for("decision", rec.load_registry(proj.root))


def test_new_creates_record(proj):
    row, p = rec.new_record(proj, "decision", "Use Rust")
    assert row["id"] == "DEC-0002"
    assert row["status"] == "Draft"  # new default is the first status (Draft)
    text = p.read_text()
    lines = text.splitlines()
    assert lines[0] == f"<!-- proj:decision {row['id']} -->"  # marker comment, line 1
    assert lines[1] == f"# {row['id']}. Use Rust"  # H1 = "# ID. title" (title editable; DB wins)
    assert lines[3].startswith("<!-- proj:head:start")  # fence below the title
    assert any(x.startswith("> **status:** `✏️ Draft`") for x in lines)
    assert "## Context" in text
    # changelog shows the created entry (comment → the status emoji + "created")
    assert "- ✏️ created`" in text


def test_supersede_sets_relations_not_statuses(proj):
    row, _ = dec_obj(proj).new(proj, "New decision", supersede=["1"])
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    # supersede is an edge in the relations table (not a column); statuses stay untouched
    assert [e["target"] for e in db.supersede_of(row["id"])] == ["DEC-0001"]
    assert [e["source"] for e in db.superseded_by_of("DEC-0001")] == ["DEC-0002"]
    assert db.get(spec, "DEC-0001")["status"] == "Accepted"  # seeded 0001 starts Accepted
    assert db.get(spec, row["id"])["status"] == "Draft"
    db.close()


def test_status_validates_allowed_set(proj):
    rec.new_record(proj, "decision", "D2")
    rec.status_set(proj, "decision", "2", "Accepted")
    with pytest.raises(ValueError, match="not allowed"):
        rec.status_set(proj, "decision", "2", "Bogus")


def test_status_change_stored_in_status_columns(proj):
    row, _ = rec.new_record(proj, "decision", "D2")  # starts Draft
    rec.status_set(proj, "decision", row["id"], "Proposed")
    rec.status_set(proj, "decision", row["id"], "Accepted")
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    r = db.get(spec, row["id"])
    logs = db.logs(r["id"])
    db.close()
    # each row stores the record's status AFTER the event (the `status` column)
    assert any(e["status"] == "Proposed" for e in logs)
    assert any(e["status"] == "Accepted" for e in logs)
    assert not any("status " in (e["message"] or "") for e in logs)
    # the rendered md shows backticked changelog lines + the status legend inside
    p = next((proj.root / "docs/decisions").glob(f"{r['id']}-*.md"))
    text = p.read_text()
    assert "- " in text and "`" in text and "<details>" in text
    assert " · ".join(spec.status_display(s) for s in spec.status_allowed) in text
    # created: the row's status is the initial one, message "created"
    assert any(e["status"] == "Draft" and e["message"] == "created" for e in logs)


def test_supersede_multi_and_append(proj):
    dec_obj(proj).new(proj, "B")  # 0002
    dec_obj(proj).new(proj, "C")  # 0003
    # one new decision replaces several old ones
    row, _ = dec_obj(proj).new(proj, "Consolidate", supersede=["1", "3"])  # 0004
    db = rec.open_db(proj)
    assert [e["target"] for e in db.supersede_of(row["id"])] == ["DEC-0001", "DEC-0003"]
    assert [e["source"] for e in db.superseded_by_of("DEC-0001")] == ["DEC-0004"]
    db.close()
    # a later decision appending to the same old one keeps both refs
    dec_obj(proj).new(proj, "Even newer", supersede=["1"])  # 0005
    db = rec.open_db(proj)
    assert [e["source"] for e in db.superseded_by_of("DEC-0001")] == ["DEC-0004", "DEC-0005"]
    db.close()


def test_fk_rejects_dangling_edge(proj):
    """D8: the relations FK references records(id) — an edge to a nonexistent
    id is refused at the DB layer (dangling refs die at the write-side guards)."""
    import sqlite3
    dec_obj(proj).new(proj, "B")  # 0002
    db = rec.open_db(proj)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_relation("DEC-0002", "DEC-0099", "supersede")
    db.close()


def test_check_flags_supersede_cycle(proj):
    from proj import check as checkmod
    dec_obj(proj).new(proj, "B")  # 0002
    db = rec.open_db(proj)
    # manual cycle: 0002 → 0001 and 0001 → 0002 (both ids exist → FK passes)
    db.add_relation("DEC-0002", "DEC-0001", "supersede")
    db.add_relation("DEC-0001", "DEC-0002", "supersede")
    db.close()
    problems = checkmod.check(proj)
    assert any("cycle" in p for p in problems)


# toc/graph/link were removed (A5, Q6, 2026-11-02); emojify stays —
# the status column is emoji-only, so the emoji mapping must be case-insensitive.
def test_emojify_is_case_insensitive(proj):
    from proj.core.templates import load_registry
    spec = load_registry(proj.root)["decision"]
    assert spec.emojify("accepted") == spec.emojify("Accepted") == "✅"
    assert spec.emojify("proposed") == "🌱"
