"""E1 (hard-parts-impl §0.1): events.py — wire format, HLC, append/read."""
from __future__ import annotations

import json

import pytest

from proj import events as ev


def _ev(op="set", rec="u1", at=1_000, actor="alex", seq=1, **kw):
    return ev.make(op, actor=actor, at=at, seq=seq, rec=rec, **kw)


def test_to_line_sorted_keys_single_line_trailing_newline():
    line = ev.to_line(_ev(fields={"a": "中"}, z=1, b=2))
    assert line.endswith("\n") and "\n" not in line[:-1]
    obj = json.loads(line)
    assert list(obj) == sorted(obj)  # keys sorted
    assert " " not in line.replace('"中"', "")  # no extra whitespace
    assert ev.from_line(line) == obj


def test_from_line_rejects_non_object():
    with pytest.raises(ValueError, match="JSON object"):
        ev.from_line("[1, 2]\n")


def test_make_carries_common_fields():
    got = ev.make("create", actor="b", at=5, seq=9, rec="u",
                  type="task", title="t")
    assert got == {"at": 5, "actor": "b", "op": "create", "rec": "u",
                   "seq": 9, "type": "task", "title": "t"}


def test_sort_key_is_total_and_deterministic():
    a = _ev(seq=2)
    b = _ev(seq=1)
    # different actor/at/rec all break ties; identical lines dedupe by sha1
    assert ev.sort_key(a) != ev.sort_key(b)
    assert ev.sort_key(a) == ev.sort_key(
        _ev(rec="u1", at=1_000, actor="alex", seq=2))
    # same fields, different payload -> different keys
    assert ev.sort_key(_ev(x=1)) != ev.sort_key(_ev(x=2))


def test_hlc_never_moves_backwards_and_beats_seen():
    clock = ev.HlcClock(last_at=5_000)
    first = clock.tick()  # now is << 5000 -> last wins
    assert first > 5_000
    second = clock.tick()
    assert second > first
    clock.advance_to(first + 100)
    assert clock.tick() > first + 100


def test_append_one_write_and_read_roundtrip(tmp_path):
    p = tmp_path / "events.jsonl"
    a, b = _ev(op="create"), _ev(op="set", at=2_000, seq=2)
    n = ev.append(p, [a, b])
    assert n == len(p.read_bytes())
    got = ev.read(p)
    assert got == [a, b]


def test_append_empty_is_noop_and_missing_file_reads_empty(tmp_path):
    p = tmp_path / "x.jsonl"
    assert ev.append(p, []) == 0
    assert not p.exists()
    assert ev.read(p) == []


def test_read_orders_by_sort_key_not_line_order(tmp_path):
    p = tmp_path / "e.jsonl"
    late = _ev(op="set", at=9_000, seq=7, actor="billy")
    early = _ev(op="set", at=1_000, seq=9, actor="alex")
    ev.append(p, [late, early])  # written out of order (a rebase can do this)
    assert ev.read(p) == [early, late]


def test_read_dedupes_identical_lines(tmp_path):
    p = tmp_path / "e.jsonl"
    a = _ev()
    ev.append(p, [a])
    ev.append(p, [a])  # union merge keeping the line twice
    assert ev.read(p) == [a]


def test_read_reports_bad_line_number(tmp_path):
    p = tmp_path / "e.jsonl"
    ev.append(p, [_ev()])
    p.write_text(p.read_text() + "{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"e\.jsonl:2: bad event line"):
        ev.read(p)


def test_read_rejects_file_without_trailing_newline(tmp_path):
    p = tmp_path / "e.jsonl"
    ev.append(p, [_ev()])
    p.write_text(p.read_text()[:-1], encoding="utf-8")
    with pytest.raises(ValueError, match="does not end with a newline"):
        ev.read(p)


def test_non_ascii_payloads_roundtrip(tmp_path):
    p = tmp_path / "e.jsonl"
    a = _ev(op="log", message="标题 修改 → T-000003", category="Changed")
    ev.append(p, [a])
    line = p.read_text(encoding="utf-8")
    assert "标题" in line  # not \uXXXX-escaped (ensure_ascii=False)
    assert ev.read(p) == [a]

# ---------------------------------------------------------------------------
# E7: validate (C6 check item; one test per problem kind)


def test_validate_clean_log_is_empty(tmp_path):
    p = tmp_path / "e.jsonl"
    ev.append(p, [_ev(), _ev(op="create", at=2_000, seq=1)])
    assert ev.validate(p) == []


def test_validate_missing_file_is_healthy(tmp_path):
    assert ev.validate(tmp_path / "nope.jsonl") == []


def test_validate_empty_file_is_healthy(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text("", encoding="utf-8")
    assert ev.validate(p) == []


def test_validate_bad_json_names_line_number(tmp_path):
    p = tmp_path / "e.jsonl"
    ev.append(p, [_ev()])
    p.write_text(p.read_text() + "{not json\n", encoding="utf-8")
    (got,) = ev.validate(p)
    assert "e.jsonl:2" in got and "bad event line" in got


def test_validate_conflict_marker(tmp_path):
    p = tmp_path / "e.jsonl"
    ev.append(p, [_ev()])
    p.write_text(p.read_text() + "<<<<<<< HEAD\n", encoding="utf-8")
    (got,) = ev.validate(p)
    assert "e.jsonl:2" in got and "conflict marker" in got


def test_validate_non_object_line(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text("[1, 2]\n", encoding="utf-8")
    (got,) = ev.validate(p)
    assert "e.jsonl:1" in got


def test_validate_missing_required_field(tmp_path):
    p = tmp_path / "e.jsonl"
    line = json.dumps(ev.make("set", actor="a", at=1, seq=1, rec="u"),
                      sort_keys=True) + "\n"
    p.write_text(line, encoding="utf-8")  # a valid object, `op` present
    e = ev.from_line(line)
    del e["seq"]  # drop one required field, keep the object valid
    p.write_text(json.dumps(e, sort_keys=True) + "\n", encoding="utf-8")
    (got,) = ev.validate(p)
    assert "e.jsonl:1" in got and "missing field(s) seq" in got


def test_validate_unknown_op(tmp_path):
    p = tmp_path / "e.jsonl"
    e = _ev(op="frobnicate")
    e.pop("op")
    p.write_text(ev.to_line({**e, "op": "frobnicate"}), encoding="utf-8")
    (got,) = ev.validate(p)
    assert "e.jsonl:1" in got and "unknown op 'frobnicate'" in got


def test_validate_collects_every_problem_not_first(tmp_path):
    p = tmp_path / "e.jsonl"
    ev.append(p, [_ev()])
    raw = p.read_bytes()
    p.write_bytes(raw + b"{bad")  # a second malformed line without a
    # final newline: two problems at once (bad line + no trailing newline)
    got = ev.validate(p)
    assert any("bad event line" in x and "e.jsonl:2" in x for x in got)
    assert any("does not end with a newline" in x for x in got)
