"""v2 decision engine: frontmatter contract, index, submit validation."""
import pytest

from proj import decision as dec
from proj import index as pindex
from proj.frontmatter import split


def _first(cfg):
    return dec._records(cfg)[0]


def test_new_creates_frontmatter_record(cfg):
    rec = dec.new(cfg, "Use Rust")
    assert rec.path.name == "0001-use-rust.md"
    meta, body = split(rec.path.read_text())
    assert meta["id"] == "0001"
    assert meta["title"] == "Use Rust"
    assert meta["status"] == "Proposed"
    assert meta["uuid"]
    assert meta["history"] == [{"date": meta["created"], "event": "created"}]
    assert "# 1. Use Rust" in body
    assert "proj-template" not in rec.path.read_text()


def test_new_increments_number(cfg):
    dec.new(cfg, "A")
    rec = dec.new(cfg, "B")
    assert rec.path.name == "0002-b.md"


def test_supersede_updates_both(cfg):
    dec.new(cfg, "A")
    rec = dec.new(cfg, "B", supersede="1")
    old = _first(cfg)
    assert old.meta["status"] == "Superseded by DEC-0002"
    assert rec.meta["status"] == "Supersedes DEC-0001"
    assert rec.meta["supersedes"] == "0001"
    # status change recorded in the old record's history
    last = old.meta["history"][-1]
    assert last["event"] == "status"
    assert last["to"] == "Superseded by DEC-0002"


def test_status_set_records_history(cfg):
    rec = dec.new(cfg, "A")
    rec = dec.set_status(cfg, "1", "Accepted")
    rec.write()
    again = _first(cfg)
    assert again.meta["status"] == "Accepted"
    last = again.meta["history"][-1]
    assert last["event"] == "status" and last["from"] == "Proposed" and last["to"] == "Accepted"


def test_status_rejects_out_of_set(cfg):
    dec.new(cfg, "A")
    with pytest.raises(ValueError, match="not allowed"):
        dec.set_status(cfg, "1", "Bogus")


def test_status_allows_supersede_form(cfg):
    dec.new(cfg, "A")
    rec = dec.set_status(cfg, "1", "Supersedes DEC-0001")  # matches template with {{id}}=0001
    rec.write()
    assert _first(cfg).meta["status"] == "Supersedes DEC-0001"


def test_validate_bad_status(cfg):
    rec = dec.new(cfg, "A")
    rec.meta["status"] = "Bogus"
    idx = pindex.load(cfg)
    problems = pindex.validate_record(cfg, rec, idx)
    assert any("Bogus" in p for p in problems)


def test_validate_id_mismatch(cfg):
    rec = dec.new(cfg, "A")
    rec.meta["id"] = "9999"
    idx = pindex.load(cfg)
    problems = pindex.validate_record(cfg, rec, idx)
    assert any("does not match filename" in p for p in problems)


def test_validate_bad_uuid(cfg):
    rec = dec.new(cfg, "A")
    rec.meta["uuid"] = "not-a-uuid"
    idx = pindex.load(cfg)
    problems = pindex.validate_record(cfg, rec, idx)
    assert any("bad uuid" in p for p in problems)


def test_validate_uuid_collision(cfg):
    a = dec.new(cfg, "A")
    b = dec.new(cfg, "B")
    b.meta["uuid"] = a.uuid
    idx = pindex.load(cfg)
    idx["records"]["decision"][a.uuid] = a.to_index(a.path.relative_to(cfg.root))
    problems = pindex.validate_record(cfg, b, idx)
    assert any("already used" in p for p in problems)


def test_rebuild_index(cfg):
    dec.new(cfg, "A")
    dec.new(cfg, "B", supersede="1")
    index, problems = pindex.rebuild(cfg)
    assert problems == []
    decs = index["records"]["decision"]
    assert len(decs) == 2
    first = next(v for v in decs.values() if v["id"] == "0001")
    assert first["status"] == "Superseded by DEC-0002"
    assert "context" in first["sections"]
    # index persisted to .proj/index.json
    import json
    on_disk = json.loads((cfg.proj_dir / "index.json").read_text())
    assert on_disk["records"]["decision"] == decs


def test_link_adds_see_also(cfg):
    dec.new(cfg, "A")
    dec.new(cfg, "B")
    dec.link(cfg, "1", ["2"])
    assert "See also: DEC-2" in _first(cfg).path.read_text()


def test_graph_edges_from_supersedes(cfg):
    dec.new(cfg, "A")
    dec.new(cfg, "B", supersede="1")
    g = dec.graph(cfg)
    assert "graph LR" in g
    assert "D0001 --> D0002" in g
