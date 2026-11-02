"""WIP.md (plans §5): the "in progress" list. Membership is registered
(wip_entries), the entry body is the record's checklist, and status switches
auto-add / auto-return; sync is the write-back path with a both-sides conflict."""
from __future__ import annotations

import json

from proj import check as checkmod
from proj.engine import item_ops
from proj.engine import store as rec
from proj.engine import wip as wipmod
from proj.record_types import task as taskmod

SECTION = "acceptance criteria"  # the task's item section (lowercased)


def _wip(cfg):
    return (cfg.root / "WIP.md").read_text(encoding="utf-8")


def _has(cfg, id_) -> bool:
    db = rec.open_db(cfg)
    try:
        return db.wip_has(id_)
    finally:
        db.close()


def _item_state(cfg, id_, no):
    db = rec.open_db(cfg)
    state = db.conn.execute(
        "SELECT state FROM items WHERE record_id=? AND no=?", (id_, no)).fetchone()
    db.close()
    return state[0] if state else None


def _item_texts(cfg, id_):
    db = rec.open_db(cfg)
    rows = db.conn.execute(
        "SELECT text FROM items WHERE record_id=?", (id_,)).fetchall()
    db.close()
    return [r[0] for r in rows]


def _logs(cfg, id_):
    db = rec.open_db(cfg)
    logs = db.logs(id_)
    db.close()
    return logs


# ---- rendering ---------------------------------------------------------------

def test_wip_md_is_rendered_and_empty_placeholder(proj):
    w = _wip(proj)
    assert "# WIP" in w and "TOOL-MANAGED except item lines" in w
    assert "_nothing in progress_" in w  # empty
    assert "_Rendered by `proj update`" in w  # footer
    assert "\n---\n" in w  # the rule separating entries from the footer


def test_wip_entry_render(proj):
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    item_ops.add(proj, row["id"], "step two")
    wipmod.wip_add(proj, [row["id"]])
    w = _wip(proj)
    assert f"## {row['id']}. Feat" in w
    assert f"<!-- proj:wip-entry {row['id']} -->" in w
    assert f"· task · [{row['id']}-feat](docs/tasks/{row['id']}-feat.md)" in w
    assert "0/2" in w  # closed/total progress
    assert "#001 step one" in w and "#002 step two" in w
    assert "_nothing in progress_" not in w
    # a ticked item re-renders the progress n/m
    item_ops.check(proj, f"{row['id']}#001")
    w = _wip(proj)
    assert "1/2" in w and "- [x] #001 ✅ step one" in w


def test_wip_render_reflects_db_not_file(proj):
    """WIP.md is a rendered view: hand-editing it does not stick (no entry)."""
    w = _wip(proj)
    (proj.root / "WIP.md").write_text(w + "## T-000999. Hand added\n",
                                      encoding="utf-8")
    rec.render_all(proj)
    assert "T-000999" not in _wip(proj)


# ---- wip add / return --------------------------------------------------------

def test_wip_add_registers_and_changelogs(proj):
    row, _ = taskmod.new(proj, "Feat")
    added, notes = wipmod.wip_add(proj, [row["id"]])
    assert added == [row["id"]] and notes == []
    assert _has(proj, row["id"])
    db = rec.open_db(proj)
    src = db.conn.execute("SELECT source FROM wip_entries").fetchone()[0]
    db.close()
    assert src == "manual"
    assert any("added to WIP" in e["message"] for e in _logs(proj, row["id"]))


def test_wip_add_duplicate_is_skipped_not_error(proj):
    row, _ = taskmod.new(proj, "Feat")
    wipmod.wip_add(proj, [row["id"]])
    added, notes = wipmod.wip_add(proj, [row["id"]])
    assert added == [] and any("already in WIP" in n for n in notes)


def test_wip_return_requires_membership(proj):
    row, _ = taskmod.new(proj, "Feat")
    try:
        wipmod.wip_return(proj, [row["id"]])
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "not in WIP" in str(e)


def test_wip_return_writes_edited_items_back(proj):
    """§5.5: return hands WIP.md's (possibly edited) checklist back first."""
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    item_ops.add(proj, row["id"], "step two")
    wipmod.wip_add(proj, [row["id"]])
    # hand-edit WIP.md: close step one (unsynced)
    (proj.root / "WIP.md").write_text(
        _wip(proj).replace("- [ ] #001 step one", "- [x] #001 step one"),
        encoding="utf-8")
    returned, _ = wipmod.wip_return(proj, [row["id"]])
    assert returned == [row["id"]] and not _has(proj, row["id"])
    assert _item_state(proj, row["id"], "001") == "done"  # the edit was stored
    assert any("returned from WIP" in e["message"] for e in _logs(proj, row["id"]))


def test_wip_entry_editable_items_only(proj):
    """entry_items_text returns the checklist run, never the footer/summary."""
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    wipmod.wip_add(proj, [row["id"]])
    w = _wip(proj)
    section = wipmod.entry_items_text(w, row["id"])
    assert section == "- [ ] #001 step one"  # footer + metadata excluded
    assert wipmod.entry_items_text(w, "T-000999") == ""  # absent entry


# ---- status switches: auto add / return --------------------------------------

def test_auto_add_on_entering_wip_status(proj):
    row, _ = taskmod.new(proj, "Feat")
    assert not _has(proj, row["id"])
    rec.status_set(proj, "task", row["id"], "In Progress")
    assert _has(proj, row["id"])
    db = rec.open_db(proj)
    src = db.conn.execute("SELECT source FROM wip_entries").fetchone()[0]
    db.close()
    assert src == "auto"
    assert any("added to WIP (auto" in e["message"] for e in _logs(proj, row["id"]))


def test_auto_return_on_entering_terminal(proj):
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    item_ops.add(proj, row["id"], "step two")
    item_ops.check(proj, f"{row['id']}#001")
    item_ops.check(proj, f"{row['id']}#002")  # both closed: the Done gate passes
    rec.status_set(proj, "task", row["id"], "In Progress")
    assert _has(proj, row["id"])
    rec.status_set(proj, "task", row["id"], "Done")
    assert not _has(proj, row["id"])
    assert any("returned from WIP (auto: Done)" in e["message"]
               for e in _logs(proj, row["id"]))


def test_same_status_operation_does_not_readd(proj):
    """A manual return is never re-triggered by a same-status op."""
    row, _ = taskmod.new(proj, "Feat")
    rec.status_set(proj, "task", row["id"], "In Progress")
    wipmod.wip_return(proj, [row["id"]])
    assert not _has(proj, row["id"])
    rec.status_set(proj, "task", row["id"], "In Progress")  # same status: no-op
    assert not _has(proj, row["id"])


# ---- sync: write-back + conflict ---------------------------------------------

def test_sync_writes_back_wip_only_drift(proj):
    """WIP.md drifted (record md did not) → the checklist is stored, the record
    md re-renders, WIP.md re-renders from the DB."""
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    item_ops.add(proj, row["id"], "step two")
    wipmod.wip_add(proj, [row["id"]])
    (proj.root / "WIP.md").write_text(
        _wip(proj).replace("- [ ] #001 step one", "- [x] #001 step one"),
        encoding="utf-8")
    problems, _ = rec.sync(proj)
    assert problems == []
    assert _item_state(proj, row["id"], "001") == "done"
    md = (proj.root / "docs/tasks/T-000001-feat.md").read_text(encoding="utf-8")
    assert "- [x] #001 ✅ step one" in md  # re-rendered from the DB
    w = _wip(proj)
    assert "- [x] #001 ✅ step one" in w  # WIP.md re-rendered from the DB too


def test_sync_no_write_back_when_wip_matches_db(proj):
    """Only the record md drifted → the record md wins; WIP.md just re-renders
    (no write-back, so the Done gate cannot fire on an unchanged checklist)."""
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    wipmod.wip_add(proj, [row["id"]])
    md = proj.root / "docs/tasks/T-000001-feat.md"
    md.write_text(md.read_text(encoding="utf-8").replace("step one", "MD-EDITED"),
                  encoding="utf-8")
    problems, _ = rec.sync(proj)
    assert problems == []
    assert any("MD-EDITED" in t for t in _item_texts(proj, row["id"]))
    assert "MD-EDITED" in _wip(proj)  # re-rendered from the DB


def test_sync_conflict_when_both_sides_drifted(proj):
    """Record md AND WIP.md both drifted → nothing is written, the record is
    kept as is, the user keeps one side."""
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    item_ops.add(proj, row["id"], "step two")
    wipmod.wip_add(proj, [row["id"]])
    md = proj.root / "docs/tasks/T-000001-feat.md"
    md.write_text(md.read_text(encoding="utf-8").replace("step one", "MD-EDITED"),
                  encoding="utf-8")
    (proj.root / "WIP.md").write_text(
        _wip(proj).replace("step two", "WIP-EDITED"), encoding="utf-8")
    problems, _ = rec.sync(proj)
    assert any("BOTH" in p for p in problems)
    assert not any("MD-EDITED" in t for t in _item_texts(proj, row["id"]))
    assert not any("WIP-EDITED" in t for t in _item_texts(proj, row["id"]))
    # keeping the WIP.md side (back to the DB value) lets the record md through
    (proj.root / "WIP.md").write_text(
        _wip(proj).replace("WIP-EDITED", "step two"), encoding="utf-8")
    problems, _ = rec.sync(proj)
    assert problems == []
    assert any("MD-EDITED" in t for t in _item_texts(proj, row["id"]))


def test_sync_keeps_record_md_intro_when_writes_back(proj):
    """The item intro lives in the record md (stored section); WIP.md only
    carries the item lines — a write-back must not drop it."""
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    md = proj.root / "docs/tasks/T-000001-feat.md"
    t = md.read_text(encoding="utf-8")
    t = t.replace("- [ ] #001 step one",
                  "Implementation notes\n- [ ] #001 step one")
    md.write_text(t, encoding="utf-8")
    rec.sync(proj)  # store the intro
    wipmod.wip_add(proj, [row["id"]])
    (proj.root / "WIP.md").write_text(
        _wip(proj).replace("- [ ] #001 step one", "- [x] #001 step one"),
        encoding="utf-8")
    problems, _ = rec.sync(proj)
    assert problems == []
    assert _item_state(proj, row["id"], "001") == "done"
    db = rec.open_db(proj)
    stored = db.get(rec.load_registry(proj.root)["task"], row["id"])
    db.close()
    sections = json.loads(stored["sections"])
    assert "Implementation notes" in sections.get(SECTION + "/intro", "")


# ---- check invariants --------------------------------------------------------

def test_check_flags_terminal_record_in_wip(proj):
    row, _ = taskmod.new(proj, "Feat")
    item_ops.add(proj, row["id"], "step one")
    item_ops.check(proj, f"{row['id']}#001")  # closed: the Done gate passes
    wipmod.wip_add(proj, [row["id"]])
    rec.status_set(proj, "task", row["id"], "Done")  # auto-return
    # simulate a missed auto-return: put the Done record back in wip_entries
    db = rec.open_db(proj)
    db.wip_add(row["id"], "auto")
    db.close()  # commits
    problems = checkmod.check(proj)
    assert any("terminal" in p and row["id"] in p for p in problems)


def test_check_flags_wip_md_drift(proj):
    row, _ = taskmod.new(proj, "Feat")
    wipmod.wip_add(proj, [row["id"]])
    (proj.root / "WIP.md").write_text(
        _wip(proj) + "## T-000999. Hand edit\n", encoding="utf-8")
    problems = checkmod.check(proj)
    assert any("WIP.md differs" in p for p in problems)


# ---- CLI ----------------------------------------------------------------------

def test_cli_wip_add_ls_return(proj):
    from typer.testing import CliRunner

    from proj.cli import app
    r = CliRunner()
    row, _ = taskmod.new(proj, "Feat")
    res = r.invoke(app, ["--root", str(proj.root), "wip", "add", row["id"]])
    assert res.exit_code == 0 and row["id"] in res.output, res.output
    res = r.invoke(app, ["--root", str(proj.root), "wip", "add", row["id"]])
    assert res.exit_code == 0 and "already in WIP" in res.output, res.output
    res = r.invoke(app, ["--root", str(proj.root), "wip", "ls"])
    assert res.exit_code == 0 and row["id"] in res.output, res.output
    res = r.invoke(app, ["--root", str(proj.root), "wip", "return", row["id"]])
    assert res.exit_code == 0 and "returned" in res.output, res.output
    res = r.invoke(app, ["--root", str(proj.root), "wip", "return", row["id"]])
    assert res.exit_code != 0 and "not in WIP" in res.output


# ---- DB store -----------------------------------------------------------------

def test_wip_pos_append_and_holes(proj):
    """pos appends in insertion order; a removal leaves a hole (no renumber)."""
    a, _ = taskmod.new(proj, "A")
    b, _ = taskmod.new(proj, "B")
    wipmod.wip_add(proj, [a["id"], b["id"]])
    wipmod.wip_return(proj, [a["id"]])
    db = rec.open_db(proj)
    entries = db.wip_all()
    db.close()
    assert [e["record_id"] for e in entries] == [b["id"]]  # a removed, no renumber
    c, _ = taskmod.new(proj, "C")
    wipmod.wip_add(proj, [c["id"]])
    db = rec.open_db(proj)
    pos = {e["record_id"]: e["pos"] for e in db.wip_all()}
    db.close()
    assert pos[c["id"]] > pos[b["id"]]  # c appended after b despite the hole


# ---- D8: wip ls --owner (§20) -------------------------------------------------

def test_wip_ls_owner_filters_and_lists_items(proj):
    """`wip ls --owner X`: only the WIP entries that carry an OPEN item owned by
    X, with those items listed under their entry. The owner field on the WIP
    checklist (task = Acceptance Criteria) is what filters — not a record field."""
    a, _ = taskmod.new(proj, "Feat A")
    b, _ = taskmod.new(proj, "Feat B")
    wipmod.wip_add(proj, [a["id"], b["id"]])
    item_ops.add(proj, a["id"], "step for alex", owner="alex")
    item_ops.add(proj, a["id"], "step for billy", owner="billy")
    item_ops.add(proj, b["id"], "step for cindy", owner="cindy")
    item_ops.check(proj, f"{a['id']}#001")  # alex's item closed: no longer "open"
    assert wipmod.wip_ls(proj, "alex") == []  # nothing open left for alex
    # an open item: a different owner shows up
    rows = wipmod.wip_ls(proj, "billy")
    assert len(rows) == 1 and rows[0]["id"] == a["id"]
    assert rows[0]["owner_items"] == [{"no": "002", "state": "open",
                                       "text": "step for billy", "due": ""}]
    out = wipmod.format_wip_ls(rows, "billy")
    assert a["id"] in out and "#002 step for billy" in out
    assert b["id"] not in out  # the other entry is filtered out
    # the plain `wip ls` lists both entries (no owner filter)
    assert len(wipmod.wip_ls(proj)) == 2
