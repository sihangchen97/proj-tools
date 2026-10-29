from proj.records import RecordSet

BODY = """\
# 1. First

## Status

Accepted
"""


def test_create_and_next_number(cfg):
    rs = RecordSet(cfg.adr_dir)
    p = rs.create("First", BODY, number=rs.next_number())
    assert p.name == "0001-first.md"
    assert rs.next_number() == "0002"


def test_discover_parses_title_and_status(cfg):
    rs = RecordSet(cfg.adr_dir)
    rs.create("First", BODY, number="0001")
    recs = rs.discover()
    assert len(recs) == 1
    assert recs[0].title == "First"
    assert recs[0].status == "Accepted"


def test_superseded_status_parsed(cfg):
    rs = RecordSet(cfg.adr_dir)
    rs.create("Old", BODY.replace("Accepted", "Superseded by ADR-0002"), number="0001")
    rec = rs.discover()[0]
    assert rec.status.lower() == "superseded by adr-0002"
    assert rec.superseded_by == "0002"


def test_set_status_replaces(cfg):
    rs = RecordSet(cfg.adr_dir)
    p = rs.create("Old", BODY, number="0001")
    rec = rs.discover()[0]
    rs.set_status(rec, "Deprecated")
    text = p.read_text()
    assert "Deprecated" in text
    assert "Accepted" not in text


def test_dated_next_number(cfg):
    from datetime import date
    rs = RecordSet(cfg.incidents_dir, dated=True)
    assert rs.next_number() == date.today().strftime("%Y-%m-%d")
