"""S.3: handing an item to a different owner is refused by default — the
current owner keeps it unless the taker passes --reassign (MCP: reassign).
First claim (no owner yet) and re-asserting the same owner are plain edits.
Hand-editing the owner in the md (or WIP.md) is a deliberate hand-over: sync
accepts it and names it in the notes."""
import pytest

from proj.engine import item_ops as ops
from proj.engine import store as rec
from proj.record_types import task as taskmod


def _changelog(proj, rid: str) -> str:
    db = rec.open_db(proj)
    try:
        return "\n".join(e["message"] for e in db.logs(rid))
    finally:
        db.close()


def _owner(proj, rid, no):
    from proj.engine import items
    db = rec.open_db(proj)
    try:
        it = items.load(db, rid, rec.load_registry(proj.root)["task"]).by_no(no)
    finally:
        db.close()
    return it.owner if it else None


def test_take_an_owned_item_is_refused_and_db_unchanged(proj):
    row, _ = taskmod.new(proj, "T")
    rid = row["id"]
    ops.add(proj, rid, "a", owner="alex")
    with pytest.raises(ValueError, match=r"#001 is owned by alex.*--reassign"):
        ops.edit(proj, f"{rid}#001", owner="billy")
    assert _owner(proj, rid, "001") == "alex"


@pytest.mark.render_drift
def test_reassign_succeeds_and_writes_changelog(proj):
    row, _ = taskmod.new(proj, "T")
    rid = row["id"]
    ops.add(proj, rid, "a", owner="alex")
    out = ops.edit(proj, f"{rid}#001", owner="billy", reassign=True)
    assert "billy" in out
    assert "item #001 owner alex → billy" in _changelog(proj, rid)


@pytest.mark.render_drift
def test_clearing_the_owner_is_a_handover_and_needs_reassign(proj):
    row, _ = taskmod.new(proj, "T")
    rid = row["id"]
    ops.add(proj, rid, "a", owner="alex")
    with pytest.raises(ValueError, match="--reassign"):
        ops.edit(proj, f"{rid}#001", owner="")
    out = ops.edit(proj, f"{rid}#001", owner="", reassign=True)
    assert "owner:" not in out  # the owner trailer is gone
    assert "item #001 owner alex → (cleared)" in _changelog(proj, rid)


def test_first_claim_needs_no_reassign(proj):
    row, _ = taskmod.new(proj, "T")
    rid = row["id"]
    ops.add(proj, rid, "a")
    assert "alex" in ops.edit(proj, f"{rid}#001", owner="alex")  # no owner yet
    # and re-asserting the SAME owner is a no-op edit, refused nowhere
    assert "alex" in ops.edit(proj, f"{rid}#001", owner="alex")


def test_sync_notes_a_handedited_owner(proj):
    row, _ = taskmod.new(proj, "T")
    rid = row["id"]
    ops.add(proj, rid, "a", owner="alex")
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    try:
        row = db.get(spec, rid)
    finally:
        db.close()
    p = rec.file_path(proj, spec, row)
    p.write_text(p.read_text(encoding="utf-8")
                .replace("owner: alex", "owner: billy"), encoding="utf-8")
    problems, notes = rec.sync(proj)
    assert not problems
    assert any("item #001 owner alex → billy" in n for n in notes)
    assert _owner(proj, rid, "001") == "billy"
