"""Item checklists — the authoritative spec for the `items:` block (every other
doc — store.py, the profiles' hints, docs/CLI.md — points here instead of
re-stating the rules). The `items:` block of a record template defines one
structured `##` section. Module-level functions, same style as store.py — the
CLI and MCP only call in here.

TERMS (record id vs item number): the RECORD id is {code}-{seq} (fixed-width
DIGIT suffix, e.g. T-000001 — no letter overflow, see db.next_id); the ITEM
number is the per-record #n inside one checklist. A ref combines both:
T-000001#001.

NUMBERING (G1-G7): the item number is 3 chars — no-group `001`…`999` (the
leading position is 0, up to 999 items); grouped `A01`…`Z99` (the leading
position is the GROUP letter A-Z, up to 99 items per group, up to 26 groups;
beyond that the record should be split). Numbers are NEVER reused (one counter
per record per group — deleting or moving an item frees nothing). Parsing
accepts zero-shortened refs (#1 == #001, #a1 == #A01, letters case-insensitive);
rendering is always the full 3-char form. The letter in a RECORD id means
nothing (record ids are digits only); the letter in an ITEM number means group.

MD SHAPE (G2-G3): a free mix of plain text, group headings and item lines;
the DB owns the item lines and the group letters, the rest is the user's
(kept verbatim):

    ## Acceptance Criteria

    Optional intro text, free to sit anywhere.

    - [ ] #001 error rate < 0.1% for 7 days
    A plain-text line may interleave with the items.
    - [x] #002 ✅ 3 pilot users onboarded — 5 users, see PR #88

    ### A · Backend
    - [x] #A01 ✅ API returns 200 — see PR #88
    - [ ] #A02 p99 < 1s · owner: agent-b · due: 2026-11-15

    ### B · Frontend
    - [ ] #B01 form validates

Group headings are `### {LETTER} · {group name}` (the name is required, the
LETTER is assigned by the tool). An item belongs to the NEAREST heading above
it; items before the first heading are ungrouped. Item lines are never
indented (indented `- [ ]` lines are a problem — use groups instead).
One item line (a single physical line):
    - [x | ] #{编号} {emoji + " " (only when the state's group holds >1 state)}
      {text}{" — " + note}{" · owner: " + owner}{" · due: " + due}
Parse strips right-to-left (due → owner → first " — " → #number → state emoji).

SYNC (md → DB, `merge`; one pass, nothing written when there are problems):
- a heading WITHOUT a letter gets the next unused group letter, then renders
  `### {L} · {name}`; an existing letter keeps its (possibly renamed) name.
- a heading whose letter was never allocated → problem.
- an item line whose number matches a stored item but sits under a DIFFERENT
  (or no) heading than the stored group is a cross-group move (G4): it gets a
  FRESH number (next counter of the destination group), the old number is
  void; the changelog records `item #003 → #A04`; the md line is re-rendered
  with the new number.
- a line with a number no group has ever issued (and no cross-group match) →
  problem (drop the number to add it as new); duplicate numbers (within the
  md) → problem.
- an item in the DB but not in the md → deleted (number void, changelog).
- an item in a gates.items status of the record (a closed state) may not be
  left unclosed (re-open restriction); a checked line without a state emoji
  takes the first closed state (noted).
- new (numberless) items are inserted under the current heading (or ungrouped),
  right after the line they follow; the stored section re-renders
  byte-identical (predicted numbers + re-emitted group lines + verbatim text).
- empty groups (a heading with no items) are kept.
"""
from __future__ import annotations

import contextlib
import re
from datetime import date

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.core.templates import ItemsSpec, RecordTypeSpec, resolve_record_type
from proj.engine import store as rec

# one item line: checkbox + content (any indent is a problem — groups replace it)
ITEM_LINE_RE = re.compile(r"^( *)- \[([ xX])\] (.*)$")
# the item number: 3 chars (0xx = ungrouped, Lxx = group L); parsing accepts
# zero-shortened forms (#1, #a1, #A1) — 2 digits only (99/group is the cap)
SEQ_RE = re.compile(r"#[0-9a-zA-Z]{1,3} ")
DUE_RE = re.compile(r" · due: (\d{4}-\d{2}-\d{2})$")
OWNER_RE = re.compile(r" · owner: (.+)$")
NOTE_SEP = " — "
# a group heading: `### A · name` (letter optional — a new group)
GROUP_RE = re.compile(r"^### ([A-Za-z]?) · (.+)$")
# an `###` line that looks like a group heading but is malformed (no ` · `)
GROUP_BAD_RE = re.compile(r"^### [A-Za-z]? ")

CAP_NO_GROUP = 999      # G5: items without a group
CAP_PER_GROUP = 99      # G5: items per group
CAP_GROUPS = 26         # G5: groups per record (A..Z)


# ---- spec helpers ---------------------------------------------------------

def _group(states: list, closed: bool) -> list:
    return [s for s in states if s.closed is closed]


def _state_of(it: ItemsSpec, name: str):
    return next((s for s in it.states if s.name == name), None)


def _is_closed(it: ItemsSpec, name: str) -> bool:
    s = _state_of(it, name)
    return s.closed if s is not None else False


def _num(letter: str, seq: int) -> str:
    """The 3-char item number (G1): `001`…`999` ungrouped, `A01`…`Z99`
    grouped. A '?' placeholder (gate messages) echoes through; >capacity
    raises (ops check capacity before inserting)."""
    if not isinstance(seq, int):
        return str(seq)
    if not letter:
        if seq > CAP_NO_GROUP:
            raise ValueError(f"item number #{seq} exceeds the no-group capacity "
                             f"({CAP_NO_GROUP}) — split the record")
        return f"{seq:03d}"
    if seq > CAP_PER_GROUP:
        raise ValueError(f"item #{letter}{seq} exceeds the per-group capacity "
                         f"({CAP_PER_GROUP}) — split the record")
    return f"{letter.upper()}{seq:02d}"


def _nn(seq) -> str:
    """Zero-padded item number for MESSAGES: 3 digits when no group context,
    else the number as-is (>999 as-is). A non-int echoes through."""
    if isinstance(seq, int):
        return f"{seq:03d}" if seq <= CAP_NO_GROUP else str(seq)
    return str(seq)


def _ref(record_id: str, letter: str, seq: int) -> str:
    """The full item ref: `T-000001#A01` (rendered number, upper letter)."""
    return f"{record_id}#{_num(letter, seq)}"


def _show_emoji(it: ItemsSpec, state: str) -> str:
    """The state's emoji + trailing space, only when its group holds >1 state."""
    st = _state_of(it, state)
    if st is None or not st.emoji:
        return ""
    if len(_group(it.states, st.closed)) > 1:
        return f"{st.emoji} "
    return ""


# ---- render ---------------------------------------------------------------

def render_item(it: dict, spec: ItemsSpec) -> str:
    box = "x" if _is_closed(spec, it["state"]) else " "
    line = f"- [{box}] #{_num(it['grp'], it['seq'])} {_show_emoji(spec, it['state'])}{it['text']}"
    if it.get("note"):
        line += f"{NOTE_SEP}{it['note']}"
    if it.get("owner"):
        line += f" · owner: {it['owner']}"
    if it.get("due"):
        line += f" · due: {it['due']}"
    return line  # no trailing spaces: the stored/normalized md would drop them (drift)


def render_list(db, record_id: str, spec: ItemsSpec) -> str:
    """The rendered item list for one record (pos order, group headings where
    they start); '' when it has none. Group names render as BOLD lines in
    expansion contexts (views) — see views._item_expansion."""
    items = db.items_of(record_id)
    if not items:
        return ""
    out: list[str] = []
    cur = object()
    for it_ in items:
        if it_["grp"] != cur:
            if it_["grp"] and cur is not object():
                pass  # heading gap: items reappear under the same heading only
            if it_["grp"]:
                g = db.item_group_get(record_id, it_["grp"])
                if g:
                    out.append(f"### {g['letter']} · {g['title']}")
            cur = it_["grp"]
        out.append(render_item(it_, spec))
    return "\n".join(out)


def _render_from_lines(stored: list[str], db, record_id: str,
                       it: ItemsSpec) -> str:
    """Re-render a mixed section from the DB: every non-item line is kept
    verbatim (a group heading line re-emitted with its CURRENT letter/name);
    every existing item line is re-emitted with the record's CURRENT number/
    state/note/owner/due; a numberless item line (a new item, not yet in the
    DB) is kept verbatim so sync can ingest it; an item the DB has that the md
    lost is appended at the end. Returns the section text ('' when nothing)."""
    out: list[str] = []
    seen: set[tuple] = set()
    for line in stored:
        g = GROUP_RE.match(line)
        if g:
            letter = g.group(1).upper()
            if letter:
                cur = db.item_group_get(record_id, letter)
                if cur is not None:
                    out.append(f"### {cur['letter']} · {cur['title']}")
                    continue
            out.append(line)  # unknown group: not this record's — kept verbatim
            continue
        p = parse_item(line, it, [])
        if p is None or p["letter"] is None or p["seq"] is None:
            out.append(line)  # plain text, or a new (numberless) item
            continue
        cur = db.item_get(record_id, p["letter"].upper(), p["seq"])
        if cur is None:
            out.append(line)  # unknown number: not this record's item
            continue
        seen.add((cur["grp"], cur["seq"]))
        out.append(render_item(cur, it))
    for it_ in db.items_of(record_id):
        if (it_["grp"], it_["seq"]) not in seen:
            out.append(render_item(it_, it))
    return rec.normalize("\n".join(out))


def section_text(db, spec: RecordTypeSpec, row: dict) -> str:
    """The section's full md content: the stored (mixed) text with the item
    lines re-rendered from the DB."""
    assert spec.items is not None
    sections = json.loads(row["sections"] or "{}")
    stored = sections.get(spec.items.section.lower(), "")
    return _render_from_lines(stored.splitlines(), db, row["id"], spec.items)


import json  # noqa: E402  (used by section_text; kept local to the render block)


# ---- parse (right-to-left, one item line at a time) ------------------------

def parse_item(line: str, it: ItemsSpec, problems: list[str],
               notes: list[str] | None = None) -> dict | None:
    """Parse one item line into a partial item dict; None for a non-item line.
    A line that looks like an item but is malformed appends to `problems`; a
    checked line without a state emoji takes the group's first state and says
    so in `notes` (e.g. 'item #3: [x] without state emoji → met')."""
    m = ITEM_LINE_RE.match(line)
    if not m:
        return None
    indent, box, rest = m.groups()
    checked = box.lower() == "x"
    if indent:
        problems.append(f"item line is indented — sub-items are gone, use a "
                        f"group (`### L · name`) instead: {line!r}")
        return None
    owner = due = ""
    dm = DUE_RE.search(rest)
    if dm:
        if "due" not in it.fields:
            problems.append(f"item line has · due: but due is not an enabled field: {line!r}")
        due = dm.group(1)
        rest = rest[:dm.start()]
    om = OWNER_RE.search(rest)
    if om:
        if "owner" not in it.fields:
            problems.append(f"item line has · owner: but owner is not an enabled field: {line!r}")
        owner = om.group(1)
        rest = rest[:om.start()]
    note = ""
    if NOTE_SEP in rest:
        rest, note = rest.split(NOTE_SEP, 1)
        note = note.strip()
    letter: str | None = None
    seq: int | None = None
    sm = SEQ_RE.match(rest)
    if sm:
        n = sm.group(0)[1:]
        letter = n[0].upper() if n[0].isalpha() else ""
        seq = int(n[1:]) if len(n) > 1 else int(n)
        rest = rest[sm.end():]
    state = None
    for s in it.states:
        if s.emoji and rest.startswith(s.emoji + " "):
            state = s.name
            rest = rest[len(s.emoji) + 1:]
            break
    text = rest.strip()
    if not text:
        problems.append(f"item line has empty text: {line!r}")
        return None
    group = _group(it.states, checked)
    label = f"#{_num(letter, seq)}" if (letter is not None or seq is not None) else "new"
    cbx = f"[{('x' if checked else ' ')}]"
    if state is not None:
        if _is_closed(it, state) != checked:  # state's group ≠ checkbox's group
            problems.append(
                f"item {label}: emoji and checkbox conflict "
                f"({cbx} with {state!r}): {line!r}")
            return None
    else:
        state = group[0].name
        if len(group) > 1 and notes is not None:
            notes.append(f"item {label}: {cbx} without state emoji → {state}: {line!r}")
    return {"letter": letter, "seq": seq, "text": text, "state": state,
            "note": note, "owner": owner, "due": due, "checked": checked}


# ---- gates ----------------------------------------------------------------

def open_items(db, spec: RecordTypeSpec, row: dict) -> list[str]:
    """This record's unclosed items as '#n text' (the gates.items message)."""
    if spec.items is None:
        return []
    return [f"#{_num(i['grp'], i['seq'])} {i['text']}" for i in db.items_of(row["id"])
            if not _is_closed(spec.items, i["state"])]


# ---- sync merge (md → DB; one pass, nothing written on problems) -----------

def _promote(db, spec: RecordTypeSpec, row: dict, notes: list[str],
             source_name: str = "") -> None:
    """Auto-promote: declared promote, record in a promote_from status, and at
    least one closed item now exists → move it to promote_to + changelog + note."""
    it = spec.items
    assert it is not None
    if not (it.promote_from and it.promote_to and row["status"] in it.promote_from):
        return
    closed = any(_is_closed(it, i["state"]) for i in db.items_of(row["id"]))
    if not closed:
        return
    db.set(spec, row["id"], status=it.promote_to)
    db.add_log(row["id"], f"auto-promoted → {it.promote_to} (first closed item)", "Changed")
    notes.append(f"{source_name}: {row['id']} promoted → {it.promote_to} (first closed item)")


def _number_new_items(section_text: str, db, record_id: str, it: ItemsSpec) -> str:
    """The section with every numberless item line given its PREDICTED number:
    in md order, under the current group (the same order merge assigns, so the
    stored section re-renders byte-identical). Existing numbered lines, group
    lines and plain text pass through unchanged."""
    nexts: dict[str, int] = {}
    cur = ""
    out = []
    for line in section_text.splitlines():
        g = GROUP_RE.match(line)
        if g:
            cur = g.group(1).upper()
            out.append(line)
            continue
        p = parse_item(line, it, [])
        if p is not None and p["seq"] is None:
            last = db.item_last(record_id, cur)
            if cur:
                last = max(nexts.get(cur, 0), last)
            seq = last + 1
            if cur:
                if seq > CAP_PER_GROUP:
                    break  # merge will refuse with the capacity error
                nexts[cur] = seq
            elif seq > CAP_NO_GROUP:
                break
            box, rest = p["checked"], p["text"]
            out.append(f"- [{'x' if box else ' '}] #{_num(cur, seq)} {rest}")
            continue
        out.append(line)
    return "\n".join(out)


def _cross_group_move(db, record_id: str, p: dict, cur: str,
                      problems: list[str], notes: list[str], source_name: str,
                      it: ItemsSpec) -> dict | None:
    """G4: a line whose number matches a stored item but sits under a different
    heading (or none) than the stored group. Returns the DESTINATION dict (a
    fresh number in the destination group's counter space) or None when the
    number is genuinely unknown (the caller reports the problem)."""
    if p["letter"] is None or p["seq"] is None:
        return None
    old = db.item_get(record_id, p["letter"].upper(), p["seq"])
    if old is None or old["grp"] == cur:
        return None
    new_seq = db.item_last(record_id, cur) + 1
    if cur and new_seq > CAP_PER_GROUP:
        problems.append(f"{source_name}: {record_id}: group {cur} is full "
                        f"({CAP_PER_GROUP} items) — cannot move #{_num(old['grp'], old['seq'])} in")
        return None
    if not cur and new_seq > CAP_NO_GROUP:
        problems.append(f"{source_name}: {record_id}: the ungrouped list is full "
                        f"({CAP_NO_GROUP} items) — cannot move #{_num(old['grp'], old['seq'])} in")
        return None
    dest = {k: p[k] for k in p}
    dest["letter"] = cur or None
    dest["seq"] = new_seq
    old_ref = _ref(record_id, old["grp"], old["seq"])
    new_ref = _ref(record_id, cur, new_seq)
    notes.append(f"{source_name}: {old_ref} → {new_ref} (cross-group move)")
    return dest


def merge(db, spec: RecordTypeSpec, row: dict, section_text: str,
          notes: list[str] | None = None, source_name: str = "",
          renumber_all: bool = False) -> tuple[list[str], str]:
    """The single md → DB merge (validate + ingest in one pass). Returns
    (problems, stored_section): on problems the DB is untouched and the stored
    section is '' (the caller must not write); otherwise the stored section is
    the normalized mixed text with new (numberless) items given their predicted
    number, cross-group moves renumbered, and group headings carrying their
    current letters — it re-renders byte-identical. `renumber_all=True` (adopt)
    treats EVERY line's number as a foreign one: it becomes a new item under
    its heading (group counters start fresh)."""
    it = spec.items
    assert it is not None
    notes = notes if notes is not None else []
    rid = row["id"]
    problems: list[str] = []
    lines = section_text.splitlines()
    # pass 1: group headings + item lines (in md order)
    entries: list[dict] = []
    cur = ""
    cur_ok = True
    for line in lines:
        g = GROUP_RE.match(line)
        if g:
            letter = g.group(1).upper()
            if letter:
                if db.item_group_get(rid, letter) is None:
                    problems.append(f"{source_name}: unknown group letter {letter!r} "
                                    f"in {rid} — create it with `proj item group add` "
                                    f"(or drop the letter to get a new group)")
                    cur_ok = False
            else:
                n = db.item_groups_of(rid)
                if len(n) >= CAP_GROUPS:
                    problems.append(f"{source_name}: {rid} has {CAP_GROUPS} groups "
                                    f"(the A–Z cap) — cannot add another")
                    cur_ok = False
            entries.append({"kind": "group", "letter": letter, "title": g.group(2).strip()})
            cur = letter
            continue
        if GROUP_BAD_RE.match(line):
            problems.append(f"{source_name}: malformed group heading (expected "
                            f"`### L · name`): {line!r}")
            continue
        p = parse_item(line, it, problems, notes)
        if p is not None:
            if not cur_ok:
                problems.append(f"{source_name}: item under a problem group heading is ignored: {line!r}")
                continue
            entries.append({"kind": "item", "line": line, "cur": cur, **p})
    if problems:
        return problems, ""
    # pass 2: numbers (duplicates + cross-group moves)
    seen: set[tuple] = set()
    for e in entries:
        if e["kind"] != "item" or e["seq"] is None:
            continue
        key = (e["cur"], e["seq"])
        if key in seen:
            problems.append(f"{source_name}: duplicate item #{_num(e['cur'], e['seq'])}")
            continue
        seen.add(key)
        if renumber_all:
            continue  # adopt: every numbered line becomes a new item
        dest = _cross_group_move(db, rid, e, e["cur"], problems, notes, source_name, it)
        if dest is not None:
            e["cur"], e["letter"], e["seq"] = dest["cur"], dest["letter"], dest["seq"]
        elif db.item_get(rid, e["letter"].upper() if e["letter"] else "", e["seq"]) is None:
            problems.append(
                f"{source_name}: item #{_num(e['cur'], e['seq'])} does not exist for "
                f"{rid} — drop the number to add it as new")
    if problems:
        return problems, ""
    # pass 3: re-open restriction (a gates.items record may not end up with an
    # unclosed item after the merge)
    if row["status"] in spec.gates_items:
        offenders = [e for e in entries if e["kind"] == "item"
                     and not _is_closed(it, e["state"])]
        if offenders:
            first = offenders[0]
            label = (f"#{_num(first['cur'], first['seq'])}" if first["seq"] is not None
                     else "new item")
            problems.append(
                f"{source_name}: {rid} is {row['status']!r} (a closed state) — "
                f"{label} is unclosed; un-close the record first, then edit items")
            return problems, ""
    # pass 4: write (groups, deletes, updates, new numbers, changelog)
    # 4a: groups (new letters + renamed names; a heading removed = group deleted)
    md_groups = {e["letter"].upper() for e in entries
                 if e["kind"] == "group" and e["letter"]}
    md_titles = {e["letter"].upper(): e["title"] for e in entries
                 if e["kind"] == "group" and e["letter"]}
    db_groups = {g["letter"] for g in db.item_groups_of(rid)}
    new_groups = [e for e in entries if e["kind"] == "group" and not e["letter"]]
    if new_groups:
        pos = db.item_group_last(rid)
        for e in new_groups:
            letter = db.item_group_add(rid, e["title"])
            if letter is None:
                problems.append(f"{source_name}: {rid} has {CAP_GROUPS} groups "
                                f"(the A–Z cap) — cannot add a group")
                return problems, ""
            e["letter"] = letter
            notes.append(f"{source_name}: new group {letter} · {e['title']}")
            db_groups.add(letter)
    for letter, title in md_titles.items():
        g = db.item_group_get(rid, letter)
        if g is not None and g["title"] != title:
            db.item_group_set_title(rid, letter, title)
            notes.append(f"{source_name}: group {letter} renamed → {title}")
    for letter in sorted(db_groups - md_groups):  # heading dropped → group gone
        g = db.item_group_get(rid, letter)
        if g is None:
            continue
        cnt = sum(1 for e in entries if e["kind"] == "item" and e["cur"] == letter)
        if cnt:
            problems.append(f"{source_name}: group {letter} still holds {cnt} item(s) "
                            f"— move them (cross-group moves) before dropping the heading")
        else:
            db.item_group_delete(rid, letter)
            notes.append(f"{source_name}: group {letter} · {g['title']} deleted")
    if problems:
        return problems, ""
    # 4b: deletes (a stored item absent from the md)
    md_keys = {(e["cur"], e["seq"]) for e in entries if e["kind"] == "item" and e["seq"] is not None}
    existing = {(i["grp"], i["seq"]): i for i in db.items_of(rid)}
    for key, i in sorted(existing.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if key in md_keys:
            continue
        if renumber_all and key in {(e["cur"], e["seq"]) for e in entries
                                    if e["kind"] == "item" and e["seq"] is not None}:
            continue
        notes.append(f"{source_name}: item #{_num(key[0], key[1])} deleted ({i['text']})")
        db.item_delete(rid, key[0], key[1])
        db.add_log(rid, f"item #{_num(key[0], key[1])}: {i['text']}", "Removed")
    # 4c: updates / new numbers, in md order (pos = the md order)
    ordered: list[tuple] = []
    for e in entries:
        if e["kind"] != "item":
            continue
        if e["seq"] is not None and not renumber_all:
            old = existing.get((e["cur"], e["seq"]))
            if old is not None:
                moved = old["grp"] != e["cur"] or old["seq"] != e["seq"]
                cols = {k: e[k] for k in ("text", "state", "note", "owner", "due")
                        if e[k] != old[k]}
                if cols:
                    newly_closed = ("state" in cols and _is_closed(it, e["state"])
                                   and not _is_closed(it, old["state"]))
                    if newly_closed:
                        cols["closed_at"] = dbmod.utc_now()
                    if moved:
                        db.item_delete(rid, old["grp"], old["seq"])
                        seq = e["seq"]
                        db.item_insert(rid, grp=e["cur"], seq=seq, pos=0, depth=0,
                                       text=e["text"], state=e["state"], note=e["note"],
                                       owner=e["owner"], due=e["due"],
                                       closed_at=cols.get("closed_at", ""),
                                       commit=old.get("commit") or "")
                        db.item_bump(rid, e["cur"], seq)
                        db.add_log(rid, f"item #{_ref(rid, old['grp'], old['seq'])} → "
                                        f"#{_ref(rid, e['cur'], seq)}", "Changed")
                    else:
                        db.item_update(rid, e["cur"], e["seq"], **cols)
                        if "state" in cols:
                            db.add_log(rid, f"item #{_ref(rid, e['cur'], e['seq'])} "
                                            f"{old['state']} → {e['state']}", "Changed")
                        else:
                            db.add_log(rid, f"item #{_ref(rid, e['cur'], e['seq'])} edited",
                                       "Changed")
                ordered.append((e["cur"], e["seq"]))
            continue
        # a new item (numberless, or renumber_all)
        seq = db.item_last(rid, e["cur"]) + 1
        if e["cur"] and seq > CAP_PER_GROUP:
            problems.append(f"{source_name}: {rid}: group {e['cur']} is full "
                            f"({CAP_PER_GROUP} items)")
            return problems, ""
        if not e["cur"] and seq > CAP_NO_GROUP:
            problems.append(f"{source_name}: {rid}: the ungrouped list is full "
                            f"({CAP_NO_GROUP} items)")
            return problems, ""
        e["seq"] = seq
        db.item_insert(rid, grp=e["cur"], seq=seq, pos=0, depth=0, text=e["text"],
                       state=e["state"], note=e["note"], owner=e["owner"], due=e["due"],
                       closed_at=dbmod.utc_now() if _is_closed(it, e["state"]) else "")
        db.item_bump(rid, e["cur"], seq)
        ref = _ref(rid, e["cur"], seq)
        if renumber_all:
            db.add_log(rid, f"item {ref} (adopted: {e['text']})", "Added")
        else:
            db.add_log(rid, f"item {ref}: {e['text']}", "Added")
            notes.append(f"{source_name}: new item {ref} ({e['text']})")
        ordered.append((e["cur"], seq))
    for pos, key in enumerate(ordered):
        db.item_set_pos(rid, key[0], key[1], pos)
    _promote(db, spec, row, notes, source_name)
    if problems:
        return problems, ""
    # 4d: the stored section (verbatim text + current letters + predicted numbers)
    stored = rec.normalize(section_text)
    stored = _number_new_items(stored, db, rid, it)
    # re-emit the group lines with their (possibly new) letters
    letters = {e["letter"].upper() for e in entries
               if e["kind"] == "group" and e["letter"]}
    out = []
    for line in stored.splitlines():
        g = GROUP_RE.match(line)
        if g:
            letter = g.group(1).upper()
            if letter in letters:
                gr = db.item_group_get(rid, letter)
                out.append(f"### {gr['letter']} · {gr['title']}")
                continue
        out.append(line)
    return [], rec.normalize("\n".join(out))


def validate(db, spec: RecordTypeSpec, row: dict, section_text: str,
             source_name: str = "") -> tuple[list[str], str]:
    """Pre-write validation (kept for external callers): merge's pass 1-3 only —
    no DB writes. Returns (problems, stored_section) as merge does."""
    it = spec.items
    assert it is not None
    problems: list[str] = []
    lines = section_text.splitlines()
    entries: list[dict] = []
    cur = ""
    for line in lines:
        g = GROUP_RE.match(line)
        if g:
            entries.append({"kind": "group", "letter": g.group(1).upper()})
            cur = g.group(1).upper()
            continue
        p = parse_item(line, it, problems, None)
        if p is not None:
            entries.append({"kind": "item", "cur": cur, **p})
    if problems:
        return problems, ""
    seen: set[tuple] = set()
    for e in entries:
        if e["kind"] != "item" or e["seq"] is None:
            continue
        key = (e["cur"], e["seq"])
        if key in seen:
            problems.append(f"{source_name}: duplicate item #{_num(e['cur'], e['seq'])}")
            continue
        seen.add(key)
        if db.item_get(rid := row["id"], e["letter"].upper() if e["letter"] else "",
                       e["seq"]) is None and not _cross_group_move(
                db, rid, e, e["cur"], problems, [], source_name, it):
            problems.append(
                f"{source_name}: item #{_num(e['cur'], e['seq'])} does not exist for "
                f"{rid} — drop the number to add it as new")
    if problems:
        return problems, ""
    if row["status"] in spec.gates_items:
        offenders = [e for e in entries if e["kind"] == "item"
                     and not _is_closed(it, e["state"])]
        if offenders:
            return [f"{source_name}: {row['id']} is {row['status']!r} (a closed state) "
                    f"— an item is unclosed; un-close the record first"], ""
    numbered = _number_new_items(rec.normalize(section_text), db, row["id"], it)
    return [], numbered


def ingest(db, spec: RecordTypeSpec, row: dict, section_text: str,
           notes: list[str], source_name: str = "") -> list[str]:
    """Merge the validated md item list into the DB (thin wrapper kept for the
    existing store.py call sites; `validate` ran first with the same text)."""
    problems, _stored = merge(db, spec, row, section_text, notes, source_name)
    return problems


def adopt(db, spec: RecordTypeSpec, row: dict, section_text: str) -> list[str]:
    """Adopt: parse a foreign md's item section — every item line becomes a NEW
    item under its heading (a numbered one is renumbered); plain text lines
    stay in the md. Fresh record: group counters start at 0."""
    problems, _stored = merge(db, spec, row, section_text, renumber_all=True)
    return problems


# ---- drift (md vs DB) -------------------------------------------------------

def md_drift_one(cfg: ProjConfig, spec: RecordTypeSpec, row: dict) -> bool:
    """True when the record md's item section differs from the DB (intro and
    list compared, normalized)."""
    if spec.items is None:
        return False
    p = rec.file_path(cfg, spec, row)
    if not p.is_file():
        return False
    (mk, u), _t, raw = rec.parse_record(p.read_text(encoding="utf-8"))
    if mk != spec.record_type or not u:
        return False
    raw_sec = raw.get(spec.items.section.lower(), "")
    db = rec.open_db(cfg)
    try:
        canonical = section_text(db, spec, row)
    finally:
        db.close()
    return rec.normalize(raw_sec) != rec.normalize(canonical)


# ---- ops (per-record item commands) ------------------------------------------

@contextlib.contextmanager
def _editing(cfg: ProjConfig, ref: str, item_ref: str | None = None):
    """The ops boilerplate: open the DB, resolve the record (and, when given,
    the item ref `ID#n`), check that the record md is in sync (an unsynced
    edit refuses the op), then yield (spec, db, row[, item]); the DB closes on
    exit. ValueError on any miss."""
    db = rec.open_db(cfg)
    try:
        spec, row = rec.ref_record(cfg, db, ref)
        if row is None:
            raise ValueError(f"{ref!r} does not match any record — use the canonical id")
        if spec.items is None:
            raise ValueError(f"{spec.record_type} has no items")
        if item_ref is None:
            yield spec, db, row
            return
        rid, sep, n = item_ref.partition("#")
        if not sep or not n or not all(c.isalnum() for c in n):
            raise ValueError(
                f"{item_ref!r} is not an item ref (expected ID#nnn, e.g. T-000001#001 "
                f"or T-000001#A01)")
        letter = n[0].upper() if n[0].isalpha() else ""
        seq = int(n[1:]) if len(n) > 1 else int(n)
        it_ = db.item_get(row["id"], letter, seq)
        if it_ is None:
            raise ValueError(f"{row['id']}: item #{_num(letter, seq)} not found")
        if md_drift_one(cfg, spec, row):
            raise ValueError(
                f"{rec.file_path(cfg, spec, row).name} has unsynced edits — run proj sync first")
        yield spec, db, row, it_
    finally:
        db.close()


def _finish(cfg: ProjConfig, row: dict) -> str:
    """Re-render; returns the record's fresh item list text."""
    rec.render_all(cfg)
    db = rec.open_db(cfg)
    try:
        spec = rec.load_registry(cfg.root).get(row["type"])
        fresh = rec.by_id(cfg, spec, db, row["id"])
        return section_text(db, spec, fresh)
    finally:
        db.close()


def _closed_gate_violation(spec: RecordTypeSpec, row: dict, new_state: str | None,
                           label: str) -> str | None:
    """Refuse to un-close an item on a record in a gates.items status."""
    if row["status"] not in spec.gates_items:
        return None
    if new_state is not None and not _is_closed(spec.items, new_state):
        return (f"{row['id']} is {row['status']!r} (a closed state) — item "
                f"{label} would be unclosed; un-close the record first")
    return None


def _validate_due(due: str) -> None:
    if due:
        try:
            date.fromisoformat(due)
        except ValueError:
            raise ValueError(f"due {due!r} is not a valid date (YYYY-MM-DD)") from None


def _validate_text(text: str) -> None:
    if NOTE_SEP in text or " · " in text:
        raise ValueError(f"item text may not contain {NOTE_SEP!r} or ' · ' "
                         f"(use --note / --owner / --due for those)")


def add(cfg: ProjConfig, ref: str, text: str, group: str | None = None,
        after: int | None = None, state: str | None = None, note: str = "",
        owner: str = "", due: str = "") -> str:
    _validate_text(text)
    _validate_due(due)
    spec, db, row = _editing(cfg, ref)
    it = spec.items
    assert it is not None
    rid = row["id"]
    if group:
        group = group.upper()
        if db.item_group_get(rid, group) is None:
            raise ValueError(f"{rid}: group {group} does not exist "
                             f"(`proj item group add {rid} \"name\"`)")
    if after is not None:
        if db.item_get(rid, group or "", after) is None:
            label = f"#{_num(group or '', after)}"
            raise ValueError(f"{rid}: item {label} not found")
    if state is not None and _state_of(it, state) is None:
        raise ValueError(f"unknown state {state!r} (expected: "
                         f"{', '.join(s.name for s in it.states)})")
    state = state or it.states[0].name
    blocked = _closed_gate_violation(spec, row, state, "new item")
    if blocked:
        raise ValueError(blocked)
    seq = db.item_last(rid, group or "") + 1
    if group and seq > CAP_PER_GROUP:
        raise ValueError(f"{rid}: group {group} is full ({CAP_PER_GROUP} items)")
    if not group and seq > CAP_NO_GROUP:
        raise ValueError(f"{rid}: the ungrouped list is full ({CAP_NO_GROUP} items)")
    pos = len(db.items_of(rid))
    if after is not None:
        items = db.items_of(rid)
        pos = next(i for i, i_ in enumerate(items)
                   if i_["grp"] == (group or "") and i_["seq"] == after) + 1
    db.item_insert(rid, grp=group or "", seq=seq, pos=0, depth=0, text=text,
                   state=state, note=note, owner=owner, due=due,
                   closed_at=dbmod.utc_now() if _is_closed(it, state) else "")
    db.item_bump(rid, group or "", seq)
    items = [(i["grp"], i["seq"]) for i in db.items_of(rid)]
    key = (group or "", seq)
    items.remove(key)
    items.insert(pos, key)
    for p, k in enumerate(items):
        db.item_set_pos(rid, k[0], k[1], p)
    ref_ = _ref(rid, group or "", seq)
    db.add_log(rid, f"item {ref_}: {text}", "Added")
    _promote(db, spec, row, [], rid)
    return _finish(cfg, row)


def set_state(cfg: ProjConfig, item_ref: str, state: str, note: str | None = None,
              commit: str = "") -> str:
    spec, db, row, it_ = _resolve_editing(cfg, item_ref)
    its = spec.items
    assert its is not None
    if _state_of(its, state) is None:
        raise ValueError(f"unknown state {state!r} (expected: "
                         f"{', '.join(s.name for s in its.states)})")
    blocked = _closed_gate_violation(spec, row, state,
                                     f"#{_num(it_['grp'], it_['seq'])}")
    if blocked:
        raise ValueError(blocked)
    cols: dict = {"state": state}
    if note is not None:
        cols["note"] = note
    if _is_closed(its, state) and not _is_closed(its, it_["state"]):
        cols["closed_at"] = dbmod.utc_now()
    if commit:
        cols["commit"] = commit
    if state != it_["state"] or cols.get("note", it_["note"]) != it_["note"] or commit:
        db.item_update(row["id"], it_["grp"], it_["seq"], **cols)
        label = f"#{_num(it_['grp'], it_['seq'])}"
        if state != it_["state"]:
            db.add_log(row["id"], f"item {label} {it_['state']} → {state}", "Changed")
        else:
            db.add_log(row["id"], f"item {label} edited", "Changed")
        _promote(db, spec, row, [], row["id"])
    return _finish(cfg, row)


def check(cfg: ProjConfig, item_ref: str, note: str | None = None,
          commit: str = "") -> str:
    spec, db, _row, _it = _resolve_editing(cfg, item_ref)
    first = next(s.name for s in spec.items.states if s.closed)
    db.close()
    return set_state(cfg, item_ref, first, note=note, commit=commit)


def uncheck(cfg: ProjConfig, item_ref: str) -> str:
    spec, db, _row, _it = _resolve_editing(cfg, item_ref)
    first = spec.items.states[0].name
    db.close()
    return set_state(cfg, item_ref, first)


def edit(cfg: ProjConfig, item_ref: str, text: str | None = None,
         note: str | None = None, owner: str | None = None,
         due: str | None = None) -> str:
    if text is not None:
        _validate_text(text)
    _validate_due(due or "")
    spec, db, row, it_ = _resolve_editing(cfg, item_ref)
    its = spec.items
    assert its is not None
    if owner is not None and "owner" not in its.fields:
        raise ValueError(f"{spec.record_type} items have no owner field")
    if due is not None and "due" not in its.fields:
        raise ValueError(f"{spec.record_type} items have no due field")
    cols: dict = {}
    if text is not None:
        cols["text"] = text
    if note is not None:
        cols["note"] = note
    if owner is not None:
        cols["owner"] = owner
    if due is not None:
        cols["due"] = due
    if cols:
        db.item_update(row["id"], it_["grp"], it_["seq"], **cols)
        db.add_log(row["id"], f"item #{_num(it_['grp'], it_['seq'])} edited", "Changed")
    return _finish(cfg, row)


def move(cfg: ProjConfig, item_ref: str, group: str | None = None,
         ungroup: bool = False, after: int | None = None,
         first: bool = False) -> str:
    """Move an item: `--group L` / `--ungroup` (cross-group → fresh number,
    G4), or `--after` / `--first` (within the item's current group)."""
    spec, db, row, it_ = _resolve_editing(cfg, item_ref)
    rid = row["id"]
    if (group is not None) + ungroup + (after is not None) + first != 1:
        raise ValueError("pass exactly one of --group L / --ungroup / --after NN / --first")
    if group:
        group = group.upper()
    if after is not None:
        if db.item_get(rid, it_["grp"], after) is None:
            raise ValueError(f"{rid}: item #{_num(it_['grp'], after)} not found")
    dest_grp = group or "" if (group or ungroup) else it_["grp"]
    old_ref = _ref(rid, it_["grp"], it_["seq"])
    db.item_delete(rid, it_["grp"], it_["seq"])
    seq = db.item_last(rid, dest_grp) + 1
    if dest_grp and seq > CAP_PER_GROUP:
        raise ValueError(f"{rid}: group {dest_grp} is full ({CAP_PER_GROUP} items)")
    if not dest_grp and seq > CAP_NO_GROUP:
        raise ValueError(f"{rid}: the ungrouped list is full ({CAP_NO_GROUP} items)")
    items = [(i["grp"], i["seq"]) for i in db.items_of(rid)]
    pos = len(items)
    if after is not None:
        pos = next(i for i, k in enumerate(items)
                   if k[0] == dest_grp and k[1] == after) + 1
    elif first:
        pos = next((i for i, k in enumerate(items) if k[0] == dest_grp), len(items))
    items.insert(pos, (dest_grp, seq))
    db.item_insert(rid, grp=dest_grp, seq=seq, pos=0, depth=0, text=it_["text"],
                   state=it_["state"], note=it_["note"], owner=it_["owner"],
                   due=it_["due"], closed_at=it_["closed_at"],
                   commit=it_.get("commit") or "")
    db.item_bump(rid, dest_grp, seq)
    for p, k in enumerate(items):
        db.item_set_pos(rid, k[0], k[1], p)
    new_ref = _ref(rid, dest_grp, seq)
    if old_ref != new_ref:
        db.add_log(rid, f"item {old_ref} → {new_ref}", "Changed")
    else:
        db.add_log(rid, f"item {new_ref} moved", "Changed")
    return _finish(cfg, row)


def remove(cfg: ProjConfig, item_ref: str) -> str:
    spec, db, row, it_ = _resolve_editing(cfg, item_ref)
    label = f"#{_num(it_['grp'], it_['seq'])}"
    db.item_delete(row["id"], it_["grp"], it_["seq"])
    db.add_log(row["id"], f"item {label}: {it_['text']}", "Removed")
    return _finish(cfg, row)


def group_add(cfg: ProjConfig, ref: str, title: str) -> str:
    spec, db, row = _editing(cfg, ref)
    if not title.strip():
        raise ValueError("group title must be non-empty")
    if md_drift_one(cfg, spec, row):
        raise ValueError(f"{rec.file_path(cfg, spec, row).name} has unsynced edits — "
                         f"run proj sync first")
    letter = db.item_group_add(row["id"], title.strip())
    if letter is None:
        raise ValueError(f"{row['id']} has {CAP_GROUPS} groups (the A–Z cap)")
    db.add_log(row["id"], f"group {letter}: {title.strip()}", "Added")
    return _finish(cfg, row)


def group_rename(cfg: ProjConfig, ref: str, letter: str, title: str) -> str:
    spec, db, row = _editing(cfg, ref)
    letter = (letter or "").upper()
    if not title.strip():
        raise ValueError("group title must be non-empty")
    if db.item_group_get(row["id"], letter) is None:
        raise ValueError(f"{row['id']}: group {letter} does not exist")
    if md_drift_one(cfg, spec, row):
        raise ValueError(f"{rec.file_path(cfg, spec, row).name} has unsynced edits — "
                         f"run proj sync first")
    db.item_group_set_title(row["id"], letter, title.strip())
    db.add_log(row["id"], f"group {letter} renamed → {title.strip()}", "Changed")
    return _finish(cfg, row)


def group_rm(cfg: ProjConfig, ref: str, letter: str,
             ungroup_items: bool = False, delete_items: bool = False) -> str:
    spec, db, row = _editing(cfg, ref)
    letter = (letter or "").upper()
    g = db.item_group_get(row["id"], letter)
    if g is None:
        raise ValueError(f"{row['id']}: group {letter} does not exist")
    if md_drift_one(cfg, spec, row):
        raise ValueError(f"{rec.file_path(cfg, spec, row).name} has unsynced edits — "
                         f"run proj sync first")
    members = [i for i in db.items_of(row["id"]) if i["grp"] == letter]
    if members and not (ungroup_items or delete_items):
        raise ValueError(f"{row['id']}: group {letter} holds {len(members)} item(s) — "
                         f"pass --ungroup-items or --delete-items")
    if delete_items:
        for i in members:
            db.item_delete(row["id"], letter, i["seq"])
            db.add_log(row["id"], f"item #{_num(letter, i['seq'])}: {i['text']}", "Removed")
    else:
        for i in members:
            seq = db.item_last(row["id"], "") + 1
            db.item_delete(row["id"], letter, i["seq"])
            db.item_insert(row["id"], grp="", seq=seq, pos=i["pos"], depth=0,
                           text=i["text"], state=i["state"], note=i["note"],
                           owner=i["owner"], due=i["due"], closed_at=i["closed_at"],
                           commit=i.get("commit") or "")
            db.item_bump(row["id"], "", seq)
    db.item_group_delete(row["id"], letter)
    db.add_log(row["id"], f"group {letter} · {g['title']} removed", "Removed")
    return _finish(cfg, row)


def group_mv(cfg: ProjConfig, ref: str, letter: str,
             after: str | None = None, first: bool = False) -> str:
    spec, db, row = _editing(cfg, ref)
    letter = (letter or "").upper()
    if db.item_group_get(row["id"], letter) is None:
        raise ValueError(f"{row['id']}: group {letter} does not exist")
    if (after is not None) + first != 1:
        raise ValueError("pass exactly one of --after L / --first")
    groups = db.item_groups_of(row["id"])
    letters = [g["letter"] for g in groups]
    if after:
        if after.upper() not in letters:
            raise ValueError(f"{row['id']}: group {after.upper()} does not exist")
        letters.remove(letter)
        pos = letters.index(after.upper()) + 1
    else:
        pos = 0
    if letter in letters:
        letters = [l for l in letters if l != letter]
    letters.insert(pos, letter)
    for p, l in enumerate(letters):
        db.item_group_set_pos(row["id"], l, p)
    db.add_log(row["id"], f"group {letter} moved", "Changed")
    return _finish(cfg, row)


def _resolve_editing(cfg: ProjConfig, item_ref: str):
    """(spec, db, row, item) for an item ref `ID#n` (the ops entry point)."""
    rid, sep, n = item_ref.partition("#")
    db = rec.open_db(cfg)
    spec, row = rec.ref_record(cfg, db, rid)
    if row is None:
        db.close()
        raise ValueError(f"{rid!r} does not match any record — use the canonical id")
    if spec.items is None:
        db.close()
        raise ValueError(f"{spec.record_type} has no items")
    letter = n[0].upper() if n[:1].isalpha() else ""
    seq = int(n[1:]) if len(n) > 1 else int(n)
    it_ = db.item_get(row["id"], letter, seq)
    if it_ is None:
        db.close()
        raise ValueError(f"{row['id']}: item #{_num(letter, seq)} not found")
    if md_drift_one(cfg, spec, row):
        db.close()
        raise ValueError(f"{rec.file_path(cfg, spec, row).name} has unsynced edits — "
                         f"run proj sync first")
    return spec, db, row, it_


def ls(cfg: ProjConfig, ref: str | None = None, open_only: bool = False,
       record_type: str | None = None, owner: str | None = None,
       overdue: bool = False, group: str | None = None) -> list[str]:
    """ID#nnn  state  text  [owner]  [due] — across one record, or every record."""
    reg = rec.load_registry(cfg.root)
    if record_type:
        rt = resolve_record_type(record_type, reg)   # ValueError on an unknown type
        specs = [reg[rt]]
    else:
        specs = list(reg.values())
    db = rec.open_db(cfg)
    today = date.today().isoformat()
    out: list[str] = []
    try:
        if ref is not None:
            spec, row = rec.ref_record(cfg, db, ref)
            if row is None:
                raise ValueError(f"{ref!r} does not match any record — "
                                 f"use the canonical id")
            if spec.items is None:
                raise ValueError(f"{spec.record_type} has no items")
            specs = [spec]
        for spec in specs:
            if spec.items is None:
                continue
            rows = db.all(spec)
            for row in rows:
                for it_ in db.items_of(row["id"]):
                    if group and it_["grp"] != group.upper():
                        continue
                    closed = _is_closed(spec.items, it_["state"])
                    if open_only and closed:
                        continue
                    if owner and it_["owner"] != owner:
                        continue
                    if overdue and not (it_["due"] and it_["due"] < today and not closed):
                        continue
                    line = f"{row['id']}#{_num(it_['grp'], it_['seq'])}  {it_['state']}  {it_['text']}"
                    if it_["owner"]:
                        line += f"  {it_['owner']}"
                    if it_["due"]:
                        line += f"  {it_['due']}"
                    out.append(line)
    finally:
        db.close()
    return out
