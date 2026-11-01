"""Stage-2 items engine (plan §6): md roundtrip, sync merge, numbering, gates,
re-open restriction, promote, ops, ls filters, profile validation, lock
roundtrip. CLI/MCP wiring and the n/m views are Stage 3/4 (ALEX/after)."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from proj import check as checkmod
from proj.core import db as dbmod
from proj.core import templates as tmpl
from proj.engine import items as it
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.record_types import task as taskmod


def _rec(rt: str):
    return lambda proj: record_for(rt, rec.load_registry(proj.root))


_plan = _rec("plan")
_debt = _rec("debt")
_dec = _rec("decision")


def _new(proj, rt: str, title: str) -> str:
    row, _p = rec.new_record(proj, rt, title)
    return row["id"]


def _file(proj, rid: str, d: str = "docs/tasks") -> Path:
    return next((proj.root / d).glob(f"{rid}-*.md"))


def _section(p: Path, name: str) -> str:
    lines = p.read_text(encoding="utf-8").splitlines()
    start = next(i for i, x in enumerate(lines) if x == f"## {name}")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start + 1:end]).strip("\n")


def _write_section(p: Path, name: str, content: str) -> None:
    lines = p.read_text(encoding="utf-8").splitlines()
    start = next(i for i, x in enumerate(lines) if x == f"## {name}")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    lines[start + 1:end] = content.splitlines()
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _nums(out: str) -> list[str]:
    """The '#NN' numbers of the item lines, in rendered order."""
    return [x.split("#")[1].split()[0] for x in out.splitlines() if "#" in x]


def db_items_count(proj, rid: str) -> int:
    db = rec.open_db(proj)
    n = len(db.items_of(rid))
    db.close()
    return n


# ---- parse + render roundtrip -------------------------------------------------

def test_roundtrip_two_state_and_emoji_rule(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    it.add(proj, row["id"], "a")
    it.add(proj, row["id"], "b", note="n", owner="o", due="2026-12-31")
    sec = _section(p, "Acceptance Criteria")
    assert "- [ ] #001 a" in sec
    assert "- [ ] #002 b — n · owner: o · due: 2026-12-31" in sec
    out = it.check(proj, f"{row['id']}#001")
    assert "- [x] #001 a" in out  # task: one closed state → no emoji
    assert "- [x] #001 a" in _section(p, "Acceptance Criteria")
    out = it.uncheck(proj, f"{row['id']}#001")
    assert "- [ ] #001 a" in out
    # parse back what the md holds (right-to-left strip, 2-digit numbers)
    probs: list[str] = []
    spec = rec.load_registry(proj.root)["task"]
    p_ = it.parse_line("- [x] #002 b — n · owner: o · due: 2026-12-31", spec.items, probs)
    assert probs == []
    assert p_ == {"seq": 2, "depth": 0, "text": "b", "state": "done",
                  "note": "n", "owner": "o", "due": "2026-12-31", "checked": True}


def test_roundtrip_multi_state_emoji(proj):
    row = _new(proj, "plan", "P")
    p = _file(proj, row, "docs/plans")
    it.add(proj, row, "c1")
    it.add(proj, row, "c2")
    it.set_state(proj, f"{row}#001", "met")
    it.set_state(proj, f"{row}#002", "missed")
    out = it.add(proj, row, "c3")
    assert "- [x] #001 ✅ c1" in out
    assert "- [x] #002 ❌ c2" in out
    assert "- [ ] #003 c3" in out  # pending: single open state → no emoji
    out = it.uncheck(proj, f"{row}#001")
    assert "- [ ] #001 c1" in out
    out = it.set_state(proj, f"{row}#001", "met", note="evidence")
    assert "- [x] #001 ✅ c1 — evidence" in out
    sec = _section(p, "Success Criteria")
    assert "#001 ✅ c1 — evidence" in sec and "#002 ❌ c2" in sec


def test_roundtrip_intro_and_subitem(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "intro line\n\n- [ ] a\n- [ ] b")
    problems, notes = rec.sync(proj)
    assert problems == []
    assert notes == [f"{p.name}: new item #001 (a)", f"{p.name}: new item #002 (b)"]
    _write_section(
        p, "Acceptance Criteria",
        "intro line\n\n- [x] #001 a\n- [ ] #002 b\n  - [ ] sub · owner: o")
    problems, _ = rec.sync(proj)
    assert problems == []
    db = rec.open_db(proj)
    assert [i["depth"] for i in db.items_of(row["id"])] == [0, 0, 1]
    db.close()
    out = it.set_state(proj, f"{row['id']}#003", "done")
    assert "  - [x] #003 sub · owner: o" in out


def test_parse_conflict_bad_indent_and_mixed(proj):
    # each malformed list is checked on its own record (one problem refuses the
    # whole write, so the cases stay independent)
    row = _new(proj, "plan", "P")  # ✅ is only declared as a state emoji on plans
    p = _file(proj, row, "docs/plans")
    _write_section(p, "Success Criteria", "- [ ] #001 ✅ c")  # met (closed) but unticked
    problems, _ = rec.sync(proj)
    assert any("emoji and checkbox conflict" in x for x in problems)
    assert db_items_count(proj, row) == 0  # nothing was written
    r2 = _new(proj, "plan", "P2")
    _write_section(_file(proj, r2, "docs/plans"), "Success Criteria",
                   "- [ ] c\n    - [ ] deep")
    problems, _ = rec.sync(proj)
    assert any("indented too deep" in x for x in problems)
    assert db_items_count(proj, r2) == 0
    # mixed model: a plain-text line interleaved among items (and even a
    # numberless sub-item first) is now legal — no problem, all accepted
    r3 = _new(proj, "plan", "P3")
    _write_section(_file(proj, r3, "docs/plans"), "Success Criteria",
                   "  - [ ] sub first\nintro text after\n- [ ] top")
    problems, _notes = rec.sync(proj)
    assert problems == []
    assert db_items_count(proj, r3) == 2


# ---- sync merge ----------------------------------------------------------------

def test_sync_new_auto_numbered_and_changelog(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "- [ ] a")
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any("new item #001 (a)" in n for n in notes)
    db = rec.open_db(proj)
    i = db.item_get(row["id"], 1)
    logs = db.logs(row["id"])
    db.close()
    assert i["text"] == "a" and i["state"] == "todo"
    assert any(e["message"] == "item #001: a" and e["category"] == "Added" for e in logs)
    assert "- [ ] #001 a" in _section(p, "Acceptance Criteria")  # re-rendered with the number


def test_sync_update_text_and_state(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "- [ ] a")
    rec.sync(proj)
    _write_section(p, "Acceptance Criteria", "- [x] #001 a2")
    problems, _ = rec.sync(proj)  # updates log to the changelog, not to notes
    assert problems == []
    db = rec.open_db(proj)
    i = db.item_get(row["id"], 1)
    logs = db.logs(row["id"])
    db.close()
    assert i["text"] == "a2" and i["state"] == "done" and i["closed_at"]
    assert any("→ done" in e["message"] for e in logs)


def test_sync_delete_removed_and_noted(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "- [ ] keep\n- [ ] gone")
    rec.sync(proj)
    _write_section(p, "Acceptance Criteria", "- [x] #001 keep")
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any("item #002 deleted (gone)" in n for n in notes)
    db = rec.open_db(proj)
    assert db.item_get(row["id"], 2) is None
    assert any("Removed" in (e["category"] or "") and "#002" in e["message"]
               for e in db.logs(row["id"]))
    db.close()


def test_sync_unknown_and_duplicate_number_no_write(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "- [ ] a")
    rec.sync(proj)
    _write_section(p, "Acceptance Criteria", "- [x] #001 a\n- [ ] #099 ghost")
    problems, _ = rec.sync(proj)
    assert any("does not exist" in x for x in problems)
    db = rec.open_db(proj)
    assert db.item_get(row["id"], 99) is None
    db.close()
    _write_section(p, "Acceptance Criteria", "- [x] #001 a\n- [ ] #001 dup")
    problems, _ = rec.sync(proj)
    assert any("duplicate" in x for x in problems)


def test_sync_checked_without_emoji_defaults_and_notes(proj):
    row = _new(proj, "plan", "P")
    p = _file(proj, row, "docs/plans")
    _write_section(p, "Success Criteria", "- [ ] c1")
    rec.sync(proj)
    _write_section(p, "Success Criteria", "- [x] #001 c1")
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any("without state emoji → met" in n for n in notes)
    db = rec.open_db(proj)
    assert db.item_get(row, 1)["state"] == "met"
    db.close()


def test_sync_interleaved_text_kept(proj):
    # mixed model: a plain-text line interleaved after the first item is legal —
    # it's kept verbatim in the stored section; the item line is still merged.
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "- [ ] a\nstray text after list")
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any("new item #001 (a)" in n for n in notes)
    # the stray line survives re-render, interleaved, not dropped
    assert "stray text after list" in _section(p, "Acceptance Criteria")


def test_sync_disabled_field_problem(proj):
    row = _new(proj, "plan", "P")  # plan items have no owner/due fields
    p = _file(proj, row, "docs/plans")
    _write_section(p, "Success Criteria", "- [ ] c · owner: x")
    problems, _ = rec.sync(proj)
    assert any("not an enabled field" in x for x in problems)


def test_numbers_never_reused(proj):
    row, _ = taskmod.new(proj, "T")
    it.add(proj, row["id"], "a")
    it.add(proj, row["id"], "b")
    it.remove(proj, f"{row['id']}#002")
    out = it.add(proj, row["id"], "c")
    assert "#003 c" in out


# ---- gates.items ----------------------------------------------------------------

def test_task_done_refused_while_criterion_todo(proj):
    row, _ = taskmod.new(proj, "T")
    it.add(proj, row["id"], "a")
    with pytest.raises(ValueError, match="unclosed criterion"):
        taskmod.done(proj, row["id"])
    it.check(proj, f"{row['id']}#001")
    assert taskmod.done(proj, row["id"])["status"] == "Done"


def test_plan_archive_gate_and_shelve_without_gate(proj):
    row = _new(proj, "plan", "P")
    _plan(proj).transition(proj, "activate", row)
    it.add(proj, row, "c1")
    _plan(proj).transition(proj, "review", row)  # gates.tasks empty → passes
    with pytest.raises(ValueError, match="unclosed criterion"):
        _plan(proj).transition(proj, "archive", row)
    it.set_state(proj, f"{row}#001", "met")
    assert _plan(proj).transition(proj, "archive", row)["status"] == "Archived"
    # shelve: no items gate (a pause does not require the list done)
    p2 = _new(proj, "plan", "P2")
    _plan(proj).transition(proj, "activate", p2)
    it.add(proj, p2, "pending forever")
    _plan(proj).transition(proj, "review", p2)
    assert _plan(proj).transition(proj, "shelve", p2)["status"] == "Shelved"


def test_debt_repay_gate_and_waive_without_gate(proj):
    row = _new(proj, "debt", "D")
    _debt(proj).transition(proj, "open", row)
    it.add(proj, row, "step")
    with pytest.raises(ValueError, match="unclosed step"):
        _debt(proj).transition(proj, "repay", row)
    it.check(proj, f"{row}#001")
    assert _debt(proj).transition(proj, "repay", row)["status"] == "Repaid"
    # waive: no gate — a long-term accept does not require the steps done
    row2 = _new(proj, "debt", "D2")
    _debt(proj).transition(proj, "open", row2)
    it.add(proj, row2, "step")
    assert _debt(proj).transition(proj, "waive", row2)["status"] == "Waived"


def test_decision_accept_without_gate(proj):
    row = _new(proj, "decision", "D")
    _dec(proj).transition(proj, "accept", row)
    it.add(proj, row, "check")
    it.set_state(proj, f"{row}#001", "violated")
    # no gates.items: a violated check never blocks the state machine
    assert _dec(proj).transition(proj, "accept", row)["status"] == "Accepted"
    assert checkmod.check(proj) == []


def test_check_flags_gated_record_with_unclosed_items(proj):
    row = _new(proj, "plan", "P")
    _plan(proj).transition(proj, "activate", row)
    it.add(proj, row, "c1")
    _plan(proj).transition(proj, "review", row)
    it.set_state(proj, f"{row}#001", "met")
    _plan(proj).transition(proj, "archive", row)
    # hand-set an unclosed item on the archived record (bypassing the ops)
    db = rec.open_db(proj)
    db.item_insert(row, seq=2, pos=1, depth=0, text="late", state="pending",
                   created=dbmod.utc_now())
    db.close()
    rec.render_all(proj)
    assert any("unclosed" in p for p in checkmod.check(proj))


# ---- re-open restriction ---------------------------------------------------------

def test_reopen_restricted_on_archived_plan(proj):
    row = _new(proj, "plan", "P")
    _plan(proj).transition(proj, "activate", row)
    it.add(proj, row, "c1")
    _plan(proj).transition(proj, "review", row)
    it.set_state(proj, f"{row}#001", "met")
    _plan(proj).transition(proj, "archive", row)
    with pytest.raises(ValueError, match="closed state"):
        it.uncheck(proj, f"{row}#001")
    with pytest.raises(ValueError, match="closed state"):
        it.add(proj, row, "late criterion")
    # the md channel: untick the item, sync must refuse and write nothing
    p = _file(proj, row, "docs/plans")
    _write_section(p, "Success Criteria", "- [ ] #001 c1")
    problems, _ = rec.sync(proj)
    assert any("closed state" in x for x in problems)
    db = rec.open_db(proj)
    assert db.item_get(row, 1)["state"] == "met"
    db.close()
    rec.render_all(proj)  # re-render restores the refused md
    assert "#001 ✅ c1" in _section(p, "Success Criteria")


# ---- promote --------------------------------------------------------------------

def test_promote_task_and_debt(proj):
    row, _ = taskmod.new(proj, "T")
    assert row["status"] == "Draft"
    it.add(proj, row["id"], "a")
    it.check(proj, f"{row['id']}#001")
    db = rec.open_db(proj)
    r = rec.by_id(proj, rec.load_registry(proj.root)["task"], db, row["id"])
    assert r["status"] == "In Progress"
    db.close()
    d = _new(proj, "debt", "D")
    _debt(proj).transition(proj, "open", d)
    it.add(proj, d, "s")
    it.check(proj, f"{d}#001")
    db = rec.open_db(proj)
    r = rec.by_id(proj, rec.load_registry(proj.root)["debt"], db, d)
    assert r["status"] == "In Progress"
    db.close()


# ---- ops: drift refusal, under/after, move, remove cascade, ls -------------------

def test_ops_refused_on_unsynced_md(proj):
    row, _ = taskmod.new(proj, "T")
    it.add(proj, row["id"], "a")
    p = _file(proj, row["id"])
    # keep the number: an unticked, unnumbered line would be a NEW item to the merge
    p.write_text(p.read_text().replace("- [ ] #001 a", "- [ ] #001 a (md edited)"))
    with pytest.raises(ValueError, match="unsynced"):
        it.add(proj, row["id"], "b")
    with pytest.raises(ValueError, match="unsynced"):
        it.set_state(proj, f"{row['id']}#001", "done")
    problems, _ = rec.sync(proj)
    assert problems == []
    out = it.add(proj, row["id"], "b")
    assert "#002 b" in out


def test_add_under_and_after(proj):
    row, _ = taskmod.new(proj, "T")
    it.add(proj, row["id"], "a")
    it.add(proj, row["id"], "b")
    it.add(proj, row["id"], "c")
    out = it.add(proj, row["id"], "sub", under=1)
    assert _nums(out) == ["001", "004", "002", "003"]
    out = it.add(proj, row["id"], "mid", after=4)
    assert _nums(out) == ["001", "004", "005", "002", "003"]
    with pytest.raises(ValueError, match="top-level"):
        it.add(proj, row["id"], "x", under=4)
    with pytest.raises(ValueError, match="not found"):
        it.add(proj, row["id"], "x", after=99)


def test_move_block_and_first(proj):
    row, _ = taskmod.new(proj, "T")
    it.add(proj, row["id"], "a")
    it.add(proj, row["id"], "b")
    it.add(proj, row["id"], "sub", under=2)
    it.add(proj, row["id"], "c")
    out = it.move(proj, f"{row['id']}#002", first=True)
    lines = [x for x in out.splitlines() if x.strip().startswith("-")]
    assert lines[0].startswith("- [ ] #002 b")
    assert lines[1].startswith("  - [ ] #003 sub")  # sub-item moved with its parent
    out = it.move(proj, f"{row['id']}#002", after=1)
    lines = [x for x in out.splitlines() if x.strip().startswith("-")]
    assert lines[1].startswith("- [ ] #002 b")
    with pytest.raises(ValueError, match="exactly one"):
        it.move(proj, f"{row['id']}#001", after=2, first=True)


def test_remove_cascades_subitems(proj):
    row, _ = taskmod.new(proj, "T")
    it.add(proj, row["id"], "a")
    it.add(proj, row["id"], "b")
    it.add(proj, row["id"], "sub", under=2)
    out = it.remove(proj, f"{row['id']}#002")
    assert "also removed sub-items: #003" in out
    db = rec.open_db(proj)
    assert db.item_get(row["id"], 2) is None and db.item_get(row["id"], 3) is None
    db.close()


def test_ls_filters_and_two_digit_refs(proj):
    row, _ = taskmod.new(proj, "T")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    it.add(proj, row["id"], "a", owner="o1", due=yesterday)
    it.add(proj, row["id"], "b", owner="o2")
    it.check(proj, f"{row['id']}#001")
    assert it.ls(proj) == [f"T-000001#001  done  a  o1  {yesterday}",
                           "T-000001#002  todo  b  o2"]
    assert it.ls(proj, open_only=True) == ["T-000001#002  todo  b  o2"]
    assert it.ls(proj, owner="o2") == ["T-000001#002  todo  b  o2"]
    assert it.ls(proj, overdue=True) == []  # the overdue item is closed
    it.add(proj, row["id"], "c", due=yesterday)
    assert it.ls(proj, overdue=True) == [f"T-000001#003  todo  c  {yesterday}"]
    assert it.ls(proj, record_type="task")
    with pytest.raises(ValueError, match="unknown record_type"):
        it.ls(proj, record_type="nope")


def test_refused_for_unknown_record_or_item(proj):
    with pytest.raises(ValueError, match="does not match"):
        it.set_state(proj, "T-999999#001", "done")
    row, _ = taskmod.new(proj, "T")
    it.add(proj, row["id"], "a")
    with pytest.raises(ValueError, match="not found"):
        it.set_state(proj, f"{row['id']}#002", "done")
    with pytest.raises(ValueError, match="ID#nn"):
        it.set_state(proj, f"{row['id']}", "done")


def test_edit_clears_and_validates(proj):
    row, _ = taskmod.new(proj, "T")
    it.add(proj, row["id"], "a", note="n", owner="o", due="2026-12-31")
    out = it.edit(proj, f"{row['id']}#001", text="b", note="", owner="", due="")
    assert "- [ ] #001 b" in out
    with pytest.raises(ValueError, match="valid date"):
        it.edit(proj, f"{row['id']}#001", due="not-a-date")
    with pytest.raises(ValueError, match="may not contain"):
        it.edit(proj, f"{row['id']}#001", text="a — b")
    plan_row = _new(proj, "plan", "P")  # plan items have no owner/due fields
    it.add(proj, plan_row, "c")
    with pytest.raises(ValueError, match="no owner field"):
        it.edit(proj, f"{plan_row}#001", owner="x")


# ---- profile validation -----------------------------------------------------------

BASE = """record_type: zz
code: ZZZ
dir: docs/zz
numbered: 4
status-allowed:
  - {name: Draft}
  - {name: Closed, done: true}
sections:
  - {name: List}
"""


def test_profile_items_validation(proj):
    ok = BASE + ("items:\n  section: List\n  states: [{name: a}, "
                 "{name: b, emoji: X, closed: true}]\n")
    spec = tmpl.parse_template(ok)
    assert spec.items is not None and spec.items.section == "List"
    assert [s.name for s in spec.items.states] == ["a", "b"]
    # a missing/duplicate emoji is only an error when its group has >1 state
    with pytest.raises(ValueError, match="not a section"):
        tmpl.parse_template(
            BASE + "items:\n  section: Nope\n  states: [{name: a}, {name: b, closed: true}]\n")
    with pytest.raises(ValueError, match="first state"):
        tmpl.parse_template(
            BASE + "items:\n  section: List\n  states: [{name: a, closed: true}, {name: b}]\n")
    with pytest.raises(ValueError, match="unique"):
        tmpl.parse_template(BASE + ("items:\n  section: List\n  states: [{name: a}, "
                                   "{name: b, emoji: X, closed: true}, "
                                   "{name: c, emoji: X, closed: true}]\n"))
    with pytest.raises(ValueError, match="gates\\.items"):
        tmpl.parse_template(BASE + "gates:\n  items: [Closed]\n")


# ---- lock roundtrip ---------------------------------------------------------------

def test_lock_roundtrip_items_gates_terminal(proj):
    lock = dbmod.read_lock(proj.root)
    assert lock["schema_version"] == dbmod.SCHEMA_VERSION
    for _n, d in lock["record_types"].items():
        assert "items" in d and "gates" in d and "status_terminal" in d
    task = lock["record_types"]["task"]
    assert task["items"]["section"] == "Acceptance Criteria"
    assert task["gates"] == {"tasks": ["Done"], "items": ["Done"]}
    assert task["status_terminal"] == ["Done", "Failed"]
    specs = dbmod.specs_from_lock(lock)
    assert specs == rec.load_registry(proj.root)


# ---- adopt channel -----------------------------------------------------------------

def test_adopt_parses_items_as_new(proj):
    d = proj.root / "docs/tech-debt"
    (d / "DEB-0001-old-hack.md").write_text(
        "# Old hack\n\n## Location\nsrc/x.py\n\n## Reason\nlegacy\n"
        "## Repayment Plan\n\n- [ ] step one\n- [x] step two ✅\n",
        encoding="utf-8")
    problems, _notes = rec.adopt(proj)
    assert problems == []
    db = rec.open_db(proj)
    items = db.items_of("DEB-0001")
    db.close()
    assert [i["state"] for i in items] == ["todo", "done"]
    assert [i["seq"] for i in items] == [1, 2]
