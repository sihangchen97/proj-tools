import pytest

from proj.adr import by_number, graph, link_adr, new_adr, record_set, toc


def test_new_adr(cfg):
    p = new_adr(cfg, "Use Rust")
    assert p.name == "0001-use-rust.md"
    text = p.read_text()
    assert "# 1. Use Rust" in text
    assert "Proposed" in text


def test_supersede(cfg):
    new_adr(cfg, "Use Rust")
    p = new_adr(cfg, "Use Go", supersede="1")
    assert p.name == "0002-use-go.md"
    old = by_number(record_set(cfg), "1")
    assert "Superseded by ADR-0002" in old.path.read_text()
    assert "Supersedes ADR-0001" in p.read_text()


def test_supersede_missing(cfg):
    with pytest.raises(ValueError):
        new_adr(cfg, "X", supersede="99")


def test_link(cfg):
    new_adr(cfg, "A")
    new_adr(cfg, "B")
    link_adr(cfg, "1", ["2"])
    assert "See also: ADR-2" in by_number(record_set(cfg), "1").path.read_text()


def test_toc_and_graph(cfg):
    new_adr(cfg, "A")
    new_adr(cfg, "B", supersede="1")
    rs = record_set(cfg)
    assert "0001" in toc(rs)
    g = graph(rs)
    assert "graph LR" in g
    assert "A0001 --> A0002" in g
