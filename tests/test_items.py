"""Items engine integration (plan §4): md roundtrip, sync merge, group
numbering, cross-group move (fresh number), never-reuse, gates, re-open
restriction, promote, ops, ls filters, profile validation, lock roundtrip,
adopt. The pure model lives in tests/test_items_model.py."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from proj import check as checkmod
from proj.core import db as dbmod
from proj.core import templates as tmpl
from proj.engine import item_ops as ops
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


# ---- K3: the → task reference -------------------------------------------------

def test_task_ref_renders_and_roundtrips(proj):
    """The `→ T-xxxxxx` suffix: rendered right after the text (before the note),
    parsed back into the item's task field, and a --task "" edit clears it."""
    rid = _new(proj, "task", "X")
    other = _new(proj, "task", "Y")
    ops.add(proj, rid, "build it", note="details here", task=other)
    md = _file(proj, rid).read_text(encoding="utf-8")
    assert f"- [ ] #001 build it → {other} — details here" in md
    # roundtrip: the parse keeps the ref (the DB row carries it)
    db = rec.open_db(proj)
    try:
        rows = db.items_load(rid)
    finally:
        db.close()
    assert rows[0]["task"] == other
    # clearing the ref
    ops.edit(proj, f"{rid}#001", task="")
    md = _file(proj, rid).read_text(encoding="utf-8")
    assert f" → {other}" not in md
    assert "- [ ] #001 build it — details here" in md


def test_picked_by_renders_on_the_task(proj):
    """K4: a task's `picked by` head row is the reverse of the `→ T-…`
    references — the referencing records, linked, deduped; task-only."""
    t = _new(proj, "task", "Rerun eval")
    inc = _new(proj, "incident", "Eval flake")
    ops.add(proj, inc, "refile after the rerun", task=t)
    md = _file(proj, t).read_text(encoding="utf-8")
    assert "picked by:" in md
    assert f"[{inc}-" in md  # a link to the incident file (../incidents/…)
    # the incident's own md carries NO picked-by (it is not a task)
    inc_md = _file(proj, inc, "docs/incidents").read_text(encoding="utf-8")
    assert "picked by:" not in inc_md
    # show --json: picked_by = the referencing record ids (task only)
    from proj.engine import present
    db = rec.open_db(proj)
    spec = rec.load_registry(proj.root)["task"]
    _t, data = present.show(spec, db.get(spec, t), db)
    assert data["picked_by"] == [inc]
    plan_spec = rec.load_registry(proj.root)["plan"]
    plan = _new(proj, "plan", "P")
    _t2, plan_data = present.show(plan_spec, db.get(plan_spec, plan), db)
    assert plan_data["picked_by"] is None
    db.close()


def test_task_ref_must_name_an_existing_task(proj):
    """K3/P5: the ref must be an EXISTING record of type task — a plan id, a
    missing id, or a non-T- string are all refused."""
    rid = _new(proj, "task", "X")
    plan = _new(proj, "plan", "P")
    with pytest.raises(Exception, match=r"only tasks may be referenced"):
        ops.add(proj, rid, "c", task=plan)
    with pytest.raises(Exception, match=r"does not match any record|does not name"):
        ops.add(proj, rid, "c", task="T-999999")
    with pytest.raises(Exception, match=r"does not match any record|must be a task id"):
        ops.add(proj, rid, "c", task="not-a-task")
    # a valid ref works, and edit keeps validating the new ref
    t2 = _new(proj, "task", "Y")
    ops.add(proj, rid, "c", task=t2)
    with pytest.raises(Exception, match=r"does not match any record|does not name"):
        ops.edit(proj, f"{rid}#001", task="T-888888")


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
    return [x.split("#")[1].split()[0] for x in out.splitlines()
            if "#" in x and x.lstrip().startswith("-")]


def db_items(proj, rid: str) -> list[dict]:
    db = rec.open_db(proj)
    rows = it_items_load(db, rid)
    db.close()
    return rows


def it_items_load(db, rid: str) -> list[dict]:
    """The record's items in display order (tombstones included)."""
    return db.items_load(rid)


def db_item(proj, rid: str, no: str) -> dict | None:
    return next((r for r in db_items(proj, rid) if r["no"] == it.norm_no(no)), None)


# ---- parse + render roundtrip -------------------------------------------------

def test_roundtrip_intro_trailer_and_group(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "intro line\n\n- [ ] a\n- [ ] b")
    problems, notes = rec.sync(proj)
    assert problems == []
    assert notes == [f"{p.name}: new item #001 (a)", f"{p.name}: new item #002 (b)"]
    # a group heading (no letter yet) + a trailer under the second item
    _write_section(
        p, "Acceptance Criteria",
        """intro line

- [x] #001 a
trailer under one
- [ ] #002 b
### Backend
- [ ] sub · owner: o""")
    problems, _ = rec.sync(proj)
    assert problems == []
    rows = db_items(proj, row["id"])
    assert [it.group_of(r["no"]) for r in rows if not r["deleted"]] == ["", "", "A", "A"]
    assert db_item(proj, row["id"], "A01")["owner"] == "o"  # the tool assigned group A
    sec = _section(p, "Acceptance Criteria")
    assert "### A · Backend" in sec
    assert "#001 ✅ a" in sec and "trailer under one" in sec
    assert "- [ ] #A01 sub · owner: o" in sec


def test_parse_conflict_indent_and_group_heading(proj):
    # each malformed list is checked on its own record (one problem refuses the
    # whole write, so the cases stay independent)
    row = _new(proj, "plan", "P")  # ✅ is only declared as a state emoji on plans
    p = _file(proj, row, "docs/plans")
    _write_section(p, "Success Criteria", "- [ ] #001 ✅ c")  # met (closed) but unticked
    problems, _ = rec.sync(proj)
    assert any("checkbox is unchecked" in x for x in problems)
    assert len(db_items(proj, row)) == 0  # nothing was written
    # A1: the malformed md also blocks the NEXT write command until fixed —
    # restore a valid section so the cases below stay independent
    _write_section(p, "Success Criteria", "")
    # an indented item line is a problem (nesting is gone — use a group)
    r2 = _new(proj, "plan", "P2")
    _write_section(_file(proj, r2, "docs/plans"), "Success Criteria",
                   "- [ ] c\n  - [ ] nested")
    problems, _ = rec.sync(proj)
    assert any("no longer nest" in x for x in problems)
    assert len(db_items(proj, r2)) == 0
    _write_section(_file(proj, r2, "docs/plans"), "Success Criteria", "")
    # a plain-text line interleaved among items is legal (intro / trailer)
    r3 = _new(proj, "plan", "P3")
    _write_section(_file(proj, r3, "docs/plans"), "Success Criteria",
                   "intro text first\n- [ ] top")
    problems, _notes = rec.sync(proj)
    assert problems == []
    assert len(db_items(proj, r3)) == 1


# ---- sync merge ----------------------------------------------------------------

def test_sync_new_auto_numbered_and_changelog(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "- [ ] a")
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any("new item #001 (a)" in n for n in notes)
    i = db_item(proj, row["id"], "001")
    db = rec.open_db(proj)
    logs = db.logs(row["id"])
    db.close()
    assert i["text"] == "a" and i["state"] == "open"
    assert any(e["message"] == "item #001: a" and e["category"] == "Added" for e in logs)
    assert "- [ ] #001 a" in _section(p, "Acceptance Criteria")  # re-rendered


def test_sync_update_text_and_state(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    _write_section(p, "Acceptance Criteria", "- [ ] a")
    rec.sync(proj)
    _write_section(p, "Acceptance Criteria", "- [x] #001 a2")
    problems, _ = rec.sync(proj)  # updates log to the changelog, not to notes
    assert problems == []
    i = db_item(proj, row["id"], "001")
    db = rec.open_db(proj)
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
    problems, _ = rec.sync(proj)
    assert problems == []
    assert db_item(proj, row["id"], "002")["deleted"]  # tombstone, not reused
    db = rec.open_db(proj)
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
    assert any("unknown item" in x for x in problems)
    assert db_item(proj, row["id"], "099") is None
    _write_section(p, "Acceptance Criteria", "- [x] #001 a\n- [ ] #001 dup")
    problems, _ = rec.sync(proj)
    assert any("duplicate" in x for x in problems)


def test_sync_checked_without_emoji_defaults_and_notes(proj):
    row = _new(proj, "plan", "P")
    p = _file(proj, row, "docs/plans")
    _write_section(p, "Success Criteria", "- [ ] c1")
    rec.sync(proj)
    _write_section(p, "Success Criteria", "- [x] #001 c1")
    problems, _ = rec.sync(proj)
    assert problems == []
    assert db_item(proj, row, "001")["state"] == "done"
    assert db_item(proj, row, "001")["note"] == it.NOTE_ASSIGNED
    i = db_item(proj, row, "001")
    assert i["state"] == "done"


@pytest.mark.parametrize(
    ("n_items", "groups", "md", "moved", "want_note", "want_text"),
    [
        # target group is the LAST one: #002 (ungrouped) → group A
        (2, ["Backend"], "- [x] #001 a\n### A · Backend\n- [ ] #002 b",
         "002", "#002 → #A01", "b"),
        # R1: target group A is FOLLOWED by group B: #001 (ungrouped) → A
        (1, ["G", "H"], "### A · G\n- [ ] #001 a\n\n### B · H\n",
         "001", "#001 → #A01", "a"),
    ],
)
def test_sync_cross_group_move_renumbers(proj, n_items, groups, md, moved,
                                         want_note, want_text):
    # PLAN §4.3 + R1: moving a numbered line under a different heading in the md
    # → a FRESH number (the old one is tombstoned, never reused). Parametrized
    # over the target group being the last one vs followed by another group — R1
    # used to tombstone the moved row, then block every write via auto-sync.
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    for name in ("a", "b")[:n_items]:
        ops.add(proj, row["id"], name)
    for g in groups:
        ops.group_add(proj, row["id"], g)
    _write_section(p, "Acceptance Criteria", md)
    problems, notes = rec.sync(proj)
    assert problems == []
    assert any(want_note in n for n in notes)
    assert db_item(proj, row["id"], moved)["deleted"]   # old number is a tombstone
    a1 = db_item(proj, row["id"], "A01")
    assert a1 is not None and a1["text"] == want_text and not a1["deleted"]  # stays live
    assert f"#A01 {want_text}" in p.read_text(encoding="utf-8")
    ops.add(proj, row["id"], "post-move")               # writes are not blocked


def test_sync_unknown_group_letter_problem(proj):
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    ops.add(proj, row["id"], "a")
    _write_section(p, "Acceptance Criteria", "- [x] #001 a\n### Q · nope\n- [ ] b")
    problems, _ = rec.sync(proj)
    assert any("unknown group" in x for x in problems)
    assert len(db_items(proj, row["id"])) == 1  # nothing written


# ---- gates.items ----------------------------------------------------------------

def test_task_done_refused_while_criterion_todo(proj):
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a")
    taskmod.start(proj, row["id"])  # S.9: done leaves only In Progress
    with pytest.raises(ValueError, match="unclosed criterion"):
        taskmod.done(proj, row["id"])
    ops.check(proj, f"{row['id']}#001")
    assert taskmod.done(proj, row["id"])["status"] == "Done"


def test_plan_archive_gate_and_shelve_without_gate(proj):
    row = _new(proj, "plan", "P")
    _plan(proj).transition(proj, "activate", row)
    ops.add(proj, row, "c1")
    _plan(proj).transition(proj, "review", row)  # gates.tasks empty → passes
    with pytest.raises(ValueError, match="unclosed criterion"):
        _plan(proj).transition(proj, "archive", row)
    ops.set_state(proj, f"{row}#001", "done")
    assert _plan(proj).transition(proj, "archive", row)["status"] == "Archived"
    # shelve: no items gate (a pause does not require the list done)
    p2 = _new(proj, "plan", "P2")
    _plan(proj).transition(proj, "activate", p2)
    ops.add(proj, p2, "pending forever")
    _plan(proj).transition(proj, "review", p2)
    assert _plan(proj).transition(proj, "shelve", p2)["status"] == "Shelved"


def test_debt_repay_gate_and_waive_without_gate(proj):
    row = _new(proj, "debt", "D")
    _debt(proj).transition(proj, "open", row)
    ops.add(proj, row, "step")
    with pytest.raises(ValueError, match="unclosed step"):
        _debt(proj).transition(proj, "repay", row)
    ops.check(proj, f"{row}#001")
    assert _debt(proj).transition(proj, "repay", row)["status"] == "Repaid"
    # waive: no gate — a long-term accept does not require the steps done
    row2 = _new(proj, "debt", "D2")
    _debt(proj).transition(proj, "open", row2)
    ops.add(proj, row2, "step")
    assert _debt(proj).transition(proj, "waive", row2)["status"] == "Waived"


def test_decision_accept_without_gate(proj):
    row = _new(proj, "decision", "D")
    _dec(proj).transition(proj, "accept", row)
    ops.add(proj, row, "check")
    ops.set_state(proj, f"{row}#001", "failed")
    # no items gate on decision: a failed check never blocks the state machine
    assert _dec(proj).transition(proj, "accept", row)["status"] == "Accepted"
    assert checkmod.check(proj) == []


def test_check_flags_gated_record_with_unclosed_items(proj):
    row = _new(proj, "plan", "P")
    _plan(proj).transition(proj, "activate", row)
    ops.add(proj, row, "c1")
    _plan(proj).transition(proj, "review", row)
    ops.set_state(proj, f"{row}#001", "done")
    _plan(proj).transition(proj, "archive", row)
    # hand-set an unclosed item on the archived record (bypassing the ops)
    db = rec.open_db(proj)
    rows = db.items_load(row)
    now = dbmod.utc_now()
    rows.append({"no": "002", "text": "late", "state": "open", "note": "",
                 "owner": "", "due": "", "created": now, "closed_at": "",
                 "commit": "", "trailer": "", "deleted": 0,
                 "section": "Success Criteria", "task": ""})
    db.items_save(row, rows)
    db.close()
    rec.render_all(proj)
    assert any("unclosed" in p for p in checkmod.check(proj))


# ---- re-open restriction ---------------------------------------------------------

def test_reopen_restricted_on_archived_plan(proj):
    row = _new(proj, "plan", "P")
    _plan(proj).transition(proj, "activate", row)
    ops.add(proj, row, "c1")
    _plan(proj).transition(proj, "review", row)
    ops.set_state(proj, f"{row}#001", "done")
    _plan(proj).transition(proj, "archive", row)
    with pytest.raises(ValueError, match="requires all items closed"):
        ops.uncheck(proj, f"{row}#001")
    with pytest.raises(ValueError, match="requires all items closed"):
        ops.add(proj, row, "late criterion")
    # the md channel: untick the item, sync must refuse and write nothing
    p = _file(proj, row, "docs/plans")
    _write_section(p, "Success Criteria", "- [ ] #001 c1")
    problems, _ = rec.sync(proj)
    assert any("requires all items closed" in x for x in problems)
    assert db_item(proj, row, "001")["state"] == "done"
    rec.render_all(proj)  # re-render restores the refused md
    assert "#001 ✅ c1" in _section(p, "Success Criteria")


# ---- promote --------------------------------------------------------------------

def test_promote_task_and_debt(proj):
    row, _ = taskmod.new(proj, "T")
    assert row["status"] == "Draft"
    ops.add(proj, row["id"], "a")
    ops.check(proj, f"{row['id']}#001")
    db = rec.open_db(proj)
    r = rec.by_id(proj, rec.load_registry(proj.root)["task"], db, row["id"])
    assert r["status"] == "In Progress"
    db.close()
    d = _new(proj, "debt", "D")
    _debt(proj).transition(proj, "open", d)
    ops.add(proj, d, "s")
    ops.check(proj, f"{d}#001")
    db = rec.open_db(proj)
    r = rec.by_id(proj, rec.load_registry(proj.root)["debt"], db, d)
    assert r["status"] == "In Progress"
    db.close()


# ---- ops: drift refusal, group/after, move, remove, ls --------------------------

def test_ops_auto_sync_unsynced_md(proj):
    """A1: an unsynced hand edit is ingested by the next write command (not
    refused, not clobbered) — the edit lands in the DB and the op proceeds."""
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a")
    p = _file(proj, row["id"])
    # keep the number: an unticked, unnumbered line would be a NEW item to the merge
    p.write_text(p.read_text().replace("- [ ] #001 a", "- [ ] #001 a (md edited)"))
    # A1: add auto-syncs the hand edit first — the op succeeds and the edit is kept
    out = ops.add(proj, row["id"], "b")
    assert "#002 b" in out
    assert db_item(proj, row["id"], "001")["text"] == "a (md edited)"
    # a malformed unsynced edit (duplicate number) aborts the next write command
    p.write_text(p.read_text().replace("- [ ] #002 b",
                                       "- [ ] #002 b\n- [ ] #002 dup"))  # duplicate → problem
    with pytest.raises(ValueError, match="unsynced md edits"):
        ops.set_state(proj, f"{row['id']}#001", "done")
    assert db_item(proj, row["id"], "001")["state"] == "open"  # untouched


def test_add_group_and_after(proj):
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a")
    ops.add(proj, row["id"], "b")
    ops.add(proj, row["id"], "c")
    out = ops.group_add(proj, row["id"], "A group")  # → group A (empty, kept)
    assert "### A · A group" in out
    out = ops.add(proj, row["id"], "mid", after=f"{row['id']}#001")
    assert _nums(out) == ["001", "004", "002", "003"]
    out = ops.add(proj, row["id"], "a two", group="A")
    assert "A01" in out
    with pytest.raises(ValueError, match="unknown group"):
        ops.add(proj, row["id"], "x", group="Z")
    with pytest.raises(ValueError, match="not an item"):
        ops.add(proj, row["id"], "x", after="999")


def test_move_between_groups_and_first(proj):
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a")
    ops.add(proj, row["id"], "b")
    ops.add(proj, row["id"], "c")
    ops.group_add(proj, row["id"], "A group")
    # cross-group move: #002 → a fresh group number (A01), old one void
    out = ops.move(proj, f"{row['id']}#002", group="A")
    assert "A01" in out and "#002" not in _nums(out)
    assert db_item(proj, row["id"], "002")["deleted"]  # tombstone, never reused
    assert db_item(proj, row["id"], "A01") is not None \
        and db_item(proj, row["id"], "A01")["text"] == "b"
    # within a group: only the order changes
    ops.add(proj, row["id"], "a two", group="A")  # A02
    out = ops.move(proj, f"{row['id']}#A02", first=True)
    assert _nums(out).index("A02") < _nums(out).index("A01")
    with pytest.raises(ValueError, match="exactly one"):
        ops.move(proj, f"{row['id']}#001", after=f"{row['id']}#002", first=True)


def test_remove_item(proj):
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a")
    ops.add(proj, row["id"], "b")
    out = ops.remove(proj, f"{row['id']}#002")
    assert "#002" not in _nums(out)
    assert db_item(proj, row["id"], "002")["deleted"]
    assert not db_item(proj, row["id"], "001")["deleted"]


def test_group_ops(proj):
    """Command-level group ops (numbering rules live in test_items_model)."""
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a")
    ops.add(proj, row["id"], "b")
    out = ops.group_add(proj, row["id"], "Backend")  # → A
    assert "### A · Backend" in out
    out = ops.group_add(proj, row["id"], "Frontend")  # → B
    assert "### B · Frontend" in out
    out = ops.group_rename(proj, row["id"], "A", "Backend API")
    assert "### A · Backend API" in out
    ops.move(proj, f"{row['id']}#002", group="B")
    # rm a non-empty group requires an explicit choice (error text)
    with pytest.raises(ValueError, match=r"ungroup|delete"):
        ops.group_rm(proj, row["id"], "B")
    # "ungroup" re-homes the items to the ungrouped list (write, not numbering)
    out = ops.group_rm(proj, row["id"], "B", "ungroup")
    assert "### B" not in out
    assert any(r["text"] == "b" for r in db_items(proj, row["id"])
               if not r["deleted"] and it.group_of(r["no"]) == "")


def test_sync_notes_move_and_groups(proj):
    # a move is ONE "#a → #b" note (not "new #b" + "#a deleted"); groups are
    # reported as groups, never as "new item #B00"
    row, _ = taskmod.new(proj, "T")
    p = _file(proj, row["id"])
    ops.add(proj, row["id"], "a")
    ops.add(proj, row["id"], "b")
    ops.group_add(proj, row["id"], "G")       # A
    _write_section(p, "Acceptance Criteria",
                   "- [ ] #002 b\n\n### A · G2\n- [ ] #001 a\n\n### H\n- [ ] c")
    problems, notes = rec.sync(proj)
    assert problems == []
    body = [n.split(": ", 1)[1] for n in notes if n.startswith(p.name)]
    assert "item #001 → #A01" in body
    assert "group A renamed: G → G2" in body
    assert "group B added: H" in body
    assert "new item #B01 (c)" in body
    assert not any("deleted" in n or "#B00" in n or "new item #A01" in n for n in body)


def test_ls_filters_and_refs(proj):
    row, _ = taskmod.new(proj, "T")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    ops.add(proj, row["id"], "a", owner="o1", due=yesterday)
    ops.add(proj, row["id"], "b", owner="o2")
    ops.check(proj, f"{row['id']}#001")
    assert ops.ls(proj) == [f"T-000001#001  done  a  o1  {yesterday}",
                            "T-000001#002  open  b  o2"]
    assert ops.ls(proj, open_only=True) == ["T-000001#002  open  b  o2"]
    assert ops.ls(proj, owner="o2") == ["T-000001#002  open  b  o2"]
    assert ops.ls(proj, overdue=True) == []  # the overdue item is closed
    ops.add(proj, row["id"], "c", due=yesterday)
    assert ops.ls(proj, overdue=True) == [f"T-000001#003  open  c  {yesterday}"]
    assert ops.ls(proj, record_type="task")
    with pytest.raises(ValueError, match="unknown record_type"):
        ops.ls(proj, record_type="nope")


def test_refused_for_unknown_record_or_item(proj):
    with pytest.raises(ValueError, match="does not match"):
        ops.set_state(proj, "T-999999#001", "done")
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a")
    with pytest.raises(ValueError, match="is not an item"):
        ops.set_state(proj, f"{row['id']}#002", "done")
    with pytest.raises(ValueError, match="#nn"):
        ops.set_state(proj, row["id"], "done")


def test_edit_clears_and_validates(proj):
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a", note="n", owner="o", due="2026-12-31")
    # S.3: clearing the owner (o → "") is a hand-over — it needs reassign=True
    out = ops.edit(proj, f"{row['id']}#001", text="b", note="", owner="", due="",
                   reassign=True)
    assert "- [ ] #001 b" in out
    with pytest.raises(ValueError, match="valid date"):
        ops.edit(proj, f"{row['id']}#001", due="not-a-date")
    with pytest.raises(ValueError, match="must not contain"):
        ops.edit(proj, f"{row['id']}#001", text="a — b")
    plan_row = _new(proj, "plan", "P")  # plan items have no owner/due fields
    ops.add(proj, plan_row, "c")
    with pytest.raises(ValueError, match="not an item field"):
        ops.edit(proj, f"{plan_row}#001", owner="x")


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
    # P4: item states are global — `states:` is rejected, section must exist;
    # P1: items: is a LIST (a bare mapping is promoted to a one-element list)
    ok = BASE + "items:\n  section: List\n  fields: [owner]\n"
    spec = tmpl.parse_template(ok)
    assert len(spec.items) == 1 and spec.items[0].section == "List"
    assert spec.items[0].closed_states == ["done", "failed", "dropped"]
    assert spec.primary_items is spec.items[0]


def test_shared_numbering_across_checklists():
    """P1: numbers (and group letters) are shared across a record's checklists —
    a second checklist sees the first's numbers as `occupied` and never reuses
    them, and allocating in the second sees the first's max."""
    from proj.engine.items import Item, ItemsState

    first = ItemsState(rows=[Item(no="001"), Item(no="002"),
                            Item(no="A00", text="g"), Item(no="A01")])
    second = ItemsState(
        rows=[Item(no="A01", section="B")],
        occupied=[r.no for r in first.rows])
    # the next ungrouped number is past the first checklist's 002
    assert second.next_no("") == "003"
    # the next group letter is past the first's A
    assert second.next_group() == "B"
    # a duplicate of the first checklist's A01 is caught by check()
    bad = ItemsState(rows=[Item(no="A02")], occupied=[r.no for r in first.rows])
    bad.rows.insert(0, Item(no="A01"))
    with pytest.raises(Exception, match=r"shared|duplicate"):
        bad.check()
    # a list with two entries: the first non-wip is primary, the wip one is wip_items
    two = (BASE.replace("sections:\n  - {name: List}",
                        "sections:\n  - {name: List}\n  - {name: A}")
           + "items:\n  - {section: List, wip: true}\n"
             "  - {section: A, gate: [Closed]}\n")
    s2 = tmpl.parse_template(two)
    assert s2.primary_items.section == "A" and s2.wip_items.section == "List"
    with pytest.raises(ValueError, match="not a section"):
        tmpl.parse_template(BASE + "items:\n  section: Nope\n")
    with pytest.raises(ValueError, match="states is gone"):
        tmpl.parse_template(BASE + ("items:\n  section: List\n  states: "
                                   "[{name: a}, {name: b, closed: true}]\n"))


# ---- lock roundtrip ---------------------------------------------------------------

def test_lock_roundtrip_items_gates_terminal(proj):
    lock = dbmod.read_lock(proj.root)
    assert lock["schema_version"] == dbmod.SCHEMA_VERSION
    for _n, d in lock["record_types"].items():
        assert "items" in d and "gates" in d and "status_terminal" in d
    task = lock["record_types"]["task"]
    assert task["items"][0]["section"] == "Acceptance Criteria"
    assert task["items"][0]["gate"] == ["Done"]
    assert task["gates"] == {"tasks": ["Done"]}  # the item gate is per-checklist
    assert task["status_terminal"] == ["Done", "Failed"]
    specs = dbmod.specs_from_lock(lock)
    assert specs == rec.load_registry(proj.root)


# ---- adopt channel -----------------------------------------------------------------

def test_adopt_parses_items_as_new(proj):
    d = proj.root / "docs/debts"
    (d / "DEB-0001-old-hack.md").write_text(
        "# Old hack\n\n## Location\nsrc/x.py\n\n## Reason\nlegacy\n"
        "## Repayment Plan\n\n- [ ] step one\n- [x] step two ✅\n",
        encoding="utf-8")
    problems, _notes = rec.adopt(proj)
    assert problems == []
    rows = db_items(proj, "DEB-0001")
    assert [i["state"] for i in rows] == ["open", "done"]
    assert [i["no"] for i in rows] == ["001", "002"]


def test_md_drifts_false_after_sync_for_item_records(proj):
    """R6: an item-bearing record is NOT flagged as drifted right after a sync —
    the items section is judged via items.drift, not as a plain text section."""
    row, _ = taskmod.new(proj, "R6")
    ops.add(proj, row["id"], "step one")
    ops.check(proj, f"{row['id']}#001")
    rec.sync(proj)
    assert rec.md_drift(proj) == [], "post-sync record must not be flagged drifted"


def test_command_output_reports_notes(proj):
    # R5: a command prints its notes (promote, renumbering) above the list
    row, _ = taskmod.new(proj, "T")
    ops.add(proj, row["id"], "a")
    out = ops.check(proj, f"{row['id']}#001")
    assert f"note: {row['id']} promoted Draft → In Progress" in out.splitlines()
    assert "- [x] #001 ✅ a" in out
    ops.group_add(proj, row["id"], "G")
    out = ops.move(proj, f"{row['id']}#001", group="A")
    assert "note: item #001 → #A01" in out
    out = ops.edit(proj, f"{row['id']}#A01", note="n")
    assert out.splitlines()[0] == "note: item #A01 edited"


# ---- P5: bare record ids in item text render as links (PLAN §22.1 D2) ------

def test_p5_bare_ids_in_item_text_render_as_links(proj):
    """A bare record id written in an item's free text becomes a link in the
    record md (no state tie); an unknown id stays bare; the drift re-render
    matches the md (no spurious drift)."""
    dec = _new(proj, "decision", "Adopt X")
    t = _new(proj, "task", "Build")
    inc = _new(proj, "incident", "Flake")
    ops.add(proj, inc, "see " + dec + " + T-999999", task=t)
    db = rec.open_db(proj)
    try:
        spec = rec.load_registry(proj.root)["incident"]
        row = db.get(spec, inc)
        state = it.load(db, inc, spec)
        state.intro = it._intro_of(spec, row)
        sec = it.render_section(state, spec)  # bare (no ctx) → ids not linked
        assert dec in sec and f"[{dec}-" not in sec
        # the record md: ids linked, dangling id bare
        md = rec.file_path(proj, spec, row).read_text(encoding="utf-8")
        assert f"[{dec}-" in md  # the bare id in the text is linked (cross-dir)
        assert f"→ {t}" in md and f"[{t}-" not in md  # K3's → ref stays bare (P3 syncs on it)
        assert "T-999999" in md and "[T-999999" not in md  # unknown id → bare
    finally:
        db.close()
    # no spurious drift: the md (with links) round-trips
    problems, _ = rec.sync(proj)
    assert not any("differs from DB" in x for x in problems), problems
