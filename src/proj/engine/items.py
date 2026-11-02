"""Item checklists: the pure model + md parsing/rendering + state transforms.

The ONE checklist per record (the `items:` section of its template) is stored
in a single `items` table, whose `no` column IS the 3-char number
(plans/PLAN.md §4, plans/items-design.md):

- '001'..'999'  ungrouped items
- 'A01'..'Z99'  items inside group A (the first letter is the group)
- 'A00'         group A itself (its `text` is the group name)

Deleted numbers are kept as tombstones (`deleted=1`) so numbers and group
letters are never reused; the next number is the max of ALL rows of its group
(tombstones included) + 1, the next letter the max of all `X00` rows + 1.

All mutations flow through the same path: load state → pure transform →
validate → diff_log → items_save → add_log (the `commit` below). sync and the
item commands share `apply_md`/`diff_log`, so both ways of editing produce
identical changelogs. This module depends only on proj.core (no store import —
`item_ops.py` is the bridge).
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from proj.core import db as dbmod
from proj.core.db import ProjError
from proj.core.templates import ItemsSpec, RecordTypeSpec

UNGROUPED_CAP = 999               # '001'..'999'
GROUP_CAP = 99                    # 'A01'..'A99'; 'A00' is the group itself
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NOTE_SEP = " — "                  # the separator before a note in an item line
NOTE_ASSIGNED = "state assigned from the checkbox (no emoji)"

# P4 (PLAN §10.3): item states are GLOBAL — one fixed set for every checklist,
# defined here (the profile no longer declares `states:`).
OPEN = "open"          # `[ ]`   unprocessed (the default)
DONE = "done"          # `[x] ✅` done / met / confirmed
FAILED = "failed"      # `[x] ❌` failed / missed / violated
DROPPED = "dropped"    # `[x] 🚫` not doing / n-a / inconclusive (reason in note)
ITEM_STATES = [OPEN, DONE, FAILED, DROPPED]
CLOSED_STATES = [DONE, FAILED, DROPPED]   # the closed half (the gate passes)
STATE_EMOJI = {DONE: "✅", FAILED: "❌", DROPPED: "🚫"}   # open shows no emoji


class ItemsError(ProjError):
    """An items problem (bad ref, unknown number, capacity, …)."""


def _spec(spec) -> ItemsSpec:
    """The ItemsSpec behind a spec. An ItemsSpec passes through; a RecordTypeSpec
    resolves to its PRIMARY checklist (the first non-`wip` one, else the first)
    — the default target for single-checklist operations. Callers that deal with
    a specific checklist (WIP, render, sync) pass that one explicitly (P1)."""
    return spec.primary_items if isinstance(spec, RecordTypeSpec) else spec


def _intro_key(section: str) -> str:
    """The sections key of a checklist's intro (kept in records.sections) —
    P1: one intro per checklist (per `##` section)."""
    return section.lower() + "/intro"


def _intro_of(spec, row: dict) -> str:
    """The intro of THIS spec's checklist (single-checklist compat helper)."""
    sections = json.loads(row.get("sections") or "{}")
    return sections.get(_intro_key(_spec(spec).section), "") or ""


def _set_intro(spec, row: dict, intro: str) -> dict:
    """row with its (this checklist's) intro updated in the sections JSON."""
    sections = json.loads(row.get("sections") or "{}")
    sections[_intro_key(_spec(spec).section)] = intro
    return {**row, "sections": json.dumps(sections)}


# ---- numbers -------------------------------------------------------------

def group_of(no: str) -> str:
    """The group letter behind a number: 'A01' → 'A'; '001' → '' (none)."""
    return no[0] if no[0].isalpha() else ""


def norm_no(s: str) -> str:
    """Normalize a number: '1'/'001' → '001'; 'a1'/'A01' → 'A01';
    'a'/'A0' → 'A00' (a bare letter names its group). Case-insensitive,
    zero padding optional. ItemsError when malformed or over capacity."""
    m = re.fullmatch(r"([a-zA-Z])(\d{0,2})|(\d{1,3})", s.strip())
    if not m:
        raise ItemsError(f"bad item number {s!r}")
    if m.group(1):
        letter = m.group(1).upper()
        if not m.group(2):
            return letter + "00"
        n = int(m.group(2))
        if n > GROUP_CAP:
            raise ItemsError(f"item number {s!r} exceeds the per-group "
                             f"capacity ({GROUP_CAP})")
        return letter + f"{n:02d}"
    n = int(m.group(3))
    if n > UNGROUPED_CAP:
        raise ItemsError(f"item number {s!r} exceeds the ungrouped capacity "
                         f"({UNGROUPED_CAP})")
    return f"{n:03d}"


def norm_group(s: str) -> str:
    """A group letter: 'A' / 'a' → 'A'; 'A00' → 'A'. ItemsError otherwise."""
    no = norm_no(s)
    if not no[0].isalpha():
        raise ItemsError(f"{s!r} is not a group (expected a letter A-Z)")
    return no[0]


# ---- model ---------------------------------------------------------------

@dataclass
class Item:
    """One row of the `items` table (including `X00` group rows and tombstones).
    `section`: which checklist (`##` section) the row belongs to (P1, multi-
    checklist); `task`: the referenced task id (P1 column; parsed from the md
    `→ T-…` suffix in K3, '' = none)."""
    no: str
    text: str = ""
    state: str = ""
    note: str = ""
    owner: str = ""
    due: str = ""
    commit: str = ""
    trailer: str = ""             # free text right below this line (kept as-is)
    created: str = ""
    closed_at: str = ""
    deleted: bool = False         # tombstone: the number is never reused
    section: str = ""
    task: str = ""

    @property
    def is_group(self) -> bool:
        """An 'A00' row (group A itself; `text` is the group name)."""
        return self.no.endswith("00") and self.no[0].isalpha()

    def to_row(self) -> dict:
        return {"no": self.no, "text": self.text, "state": self.state,
                "note": self.note, "owner": self.owner, "due": self.due,
                "commit": self.commit, "trailer": self.trailer,
                "created": self.created, "closed_at": self.closed_at,
                "deleted": self.deleted, "section": self.section,
                "task": self.task}


@dataclass
class ItemsState:
    """One checklist of a record (P1: a record may have several, one per `##`
    section): `rows` in display order (tombstones at the end, not shown).
    `intro` is the text before the first element (stored per section).
    `occupied`: the numbers (incl. group letters) used by the record's OTHER
    checklists — numbering is shared across the whole record (PLAN §10.3b), so
    next_no/next_group/check must see them."""
    intro: str = ""
    rows: list[Item] = field(default_factory=list)
    occupied: list[str] = field(default_factory=list)

    def by_no(self, no: str) -> Item | None:
        return next((r for r in self.rows if r.no == no), None)

    def _all_nos(self) -> list[str]:
        """Every number of the record: this checklist's rows (tombstones
        included) plus the numbers the other checklists use (shared)."""
        return [r.no for r in self.rows] + list(self.occupied)

    def live(self) -> list[Item]:
        """The non-deleted rows, in display order."""
        return [r for r in self.rows if not r.deleted]

    def group(self, letter: str) -> Item | None:
        """The `X00` row of a group (None when it was never allocated)."""
        return self.by_no(letter + "00")

    def next_no(self, letter: str) -> str:
        """The next number of a group (letter '' = ungrouped): the max of ALL
        numbers of that group record-wide (tombstones + the other checklists
        included — the numbering is shared) + 1. ItemsError when the capacity
        is reached."""
        cap = GROUP_CAP if letter else UNGROUPED_CAP
        seqs = [int(n[1:] if letter else n)
                for n in self._all_nos() if group_of(n) == letter]
        nxt = max(seqs, default=0) + 1
        if nxt > cap:
            where = f"group {letter}" if letter else "the ungrouped list"
            raise ItemsError(f"{where} is at capacity ({cap} items) — "
                             f"split the record")
        width = 2 if letter else 3
        return (letter or "") + f"{nxt:0{width}d}"

    def next_group(self) -> str:
        """The next free group letter: the max of all `X00` numbers record-wide
        (tombstones + the other checklists included) + 1. ItemsError past Z."""
        tops = [n[0] for n in self._all_nos()
                if n.endswith("00") and n[0].isalpha()]
        nxt = max((LETTERS.index(t) for t in tops), default=-1) + 1
        if nxt >= len(LETTERS):
            raise ItemsError(f"at most {len(LETTERS)} groups — split the record")
        return LETTERS[nxt]

    def check(self) -> None:
        """Invariants (asserted before every save):
        - display order: all ungrouped items, then each group as a block
          (its `X00` row immediately followed by that group's items);
        - numbers unique record-wide (tombstones and the other checklists
          included — the numbering is shared, PLAN §10.3b);
        - every `Xnn` item has a live `X00` row (this checklist or the other
          ones — groups are shared record-wide too);
        - tombstones come last.
        """
        rows = self.rows
        first_tomb = next((i for i, r in enumerate(rows) if r.deleted),
                          len(rows))
        if any(not r.deleted for r in rows[first_tomb:]):
            raise ItemsError("tombstones must come last")
        seen: set[str] = set(self.occupied)   # the other checklists' numbers
        cur_group: str | None = None      # None = the ungrouped section
        for r in rows[:first_tomb]:
            if r.no in seen:
                raise ItemsError(f"duplicate item number #{r.no} (numbers are "
                                 f"shared across the record's checklists)")
            seen.add(r.no)
            grp = group_of(r.no)
            if r.is_group:
                if cur_group is not None and grp != cur_group and grp in seen:
                    raise ItemsError(f"group {grp} appears twice")
                cur_group = grp
                continue
            if cur_group is not None and grp != cur_group:
                where = grp or "the ungrouped list"
                raise ItemsError(f"out-of-order item #{r.no} ({where} after "
                                 f"group {cur_group})")
            if grp and self.group(grp) is None \
                    and (grp + "00") not in self.occupied:
                raise ItemsError(f"item #{r.no} has no live group row #{grp}00")


def _new_item(no: str, text: str, **kw) -> Item:
    it = Item(no=no, text=text)
    for k, v in kw.items():
        setattr(it, k, v)
    if not it.created:
        it.created = dbmod.utc_now()
    return it


def _tomb(no: str, text: str) -> Item:
    return Item(no=no, text=text, deleted=True)


# ---- render (plans/items-design.md §4.1) -----------------------------------

def _show_emoji(state_name: str) -> str:
    """'EMOJI ' for a closed state (it always shows one); '' for open."""
    e = STATE_EMOJI.get(state_name)
    return f"{e} " if e else ""


def render_item_line(item: Item, spec) -> str:
    box = "x" if item.state in CLOSED_STATES else " "
    line = f"- [{box}] #{item.no} {_show_emoji(item.state)}{item.text}"
    if item.task:  # K3: the structured task reference (PLAN §10.3 K3)
        line += f" → {item.task}"
    if item.note:
        line += f"{NOTE_SEP}{item.note}"
    if item.owner:
        line += f" · owner: {item.owner}"
    if item.due:
        line += f" · due: {item.due}"
    return line.rstrip()


def render_section(state: ItemsState, spec) -> str:
    """The section content (without the `##` heading) — depends ONLY on state.
    `spec`: the ItemsSpec or its RecordTypeSpec."""
    spec = _spec(spec)

    def emit(r: Item) -> None:
        out.append(render_item_line(r, spec))
        if r.trailer.strip():
            out.extend(r.trailer.strip("\n").splitlines())

    out: list[str] = []
    if state.intro.strip():
        out.extend(state.intro.strip("\n").splitlines())
    for r in state.live():
        if r.is_group:
            out.append("")
            out.append(f"### {r.no[0]} · {r.text}")
            if r.trailer.strip():
                out.extend(r.trailer.strip("\n").splitlines())
        else:
            emit(r)
    return normalize("\n".join(out))


def normalize(text: str) -> str:
    """Blank-line rules (same as store.normalize, kept local — no store import):
    lines right-stripped, no leading blanks, at most one blank line, no
    trailing blanks."""
    out: list[str] = []
    for ln in (text or "").split("\n"):
        if ln.strip() == "":
            if out and out[-1] != "":
                out.append("")
        else:
            out.append(ln.rstrip())
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


# ---- parse (plans/items-design.md §4.2) ------------------------------------

GROUP_HEAD_RE = re.compile(r"^### (?:([A-Za-z]) · )?(.+)$")
ITEM_RE = re.compile(r"^- \[([ xX])\] (.*)$")
INDENT_ITEM_RE = re.compile(r"^\s+- \[[ xX]\]")
NO_RE = re.compile(r"^#([A-Za-z0-9]+)\s*(.*)$")
DUE_RE = re.compile(r" · due: (\d{4}-\d{2}-\d{2})$")
OWNER_RE = re.compile(r" · owner: (.+)$")
# K3: the structured task reference — a right-anchored ` → T-xxxxxx` suffix,
# parsed right-to-left AFTER due/owner/note (PLAN §22.1 D2). Rendered right
# after the text, before the note.
TASK_REF_RE = re.compile(r" → (T-\d{4,})$")
EMOJI_RE = re.compile(r"^(\S)\s+(.+)$")


@dataclass
class PItem:
    no: str | None                 # None = a new item
    checked: bool
    state: str = ""                # resolved (default '' when undetermined)
    text: str = ""
    note: str = ""
    owner: str = ""
    due: str = ""
    task: str = ""                 # K3: the referenced task id (the `→ T-xxxxxx` suffix)
    trailer: str = ""
    line: str = ""
    from_emoji: bool = False       # the state came from an emoji (conflict check)


@dataclass
class PGroup:
    letter: str | None             # None = unnamed (a new group)
    title: str
    trailer: str = ""
    items: list[PItem] = field(default_factory=list)


@dataclass
class Parsed:
    intro: str = ""
    ungrouped: list[PItem] = field(default_factory=list)
    groups: list[PGroup] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _all_items(p: Parsed) -> list[PItem]:
    return [*p.ungrouped, *[i for g in p.groups for i in g.items]]


def _resolve_state(it: PItem) -> None:
    """Global states (P4): an emoji pins done/failed/dropped; a bare `[x]` is
    `done` (flagged NOTE_ASSIGNED); an unchecked box is `open`."""
    m = EMOJI_RE.match(it.text)
    if m and m.group(1) in STATE_EMOJI.values():
        it.state = next(n for n, e in STATE_EMOJI.items() if e == m.group(1))
        it.text = m.group(2)
        it.from_emoji = True
        return
    it.state = DONE if it.checked else OPEN
    if it.checked and not it.note and not it.from_emoji:
        it.note = NOTE_ASSIGNED


def _trailer(cur: PItem | PGroup, buf: list[str]) -> None:
    if buf:
        cur.trailer = (cur.trailer + "\n" if cur.trailer else "") + "\n".join(buf)


def parse_section(text: str, spec) -> Parsed:
    """Scan the section content line by line into a Parsed (states resolved).
    An indented '- [ ]' line is a problem (nesting is gone); free text before
    the first element is the intro, otherwise it trails the nearest element."""
    spec = _spec(spec)
    p = Parsed()
    cur: PItem | PGroup | None = None   # the last element (trailers attach to it)
    cur_group: PGroup | None = None     # the group an item line belongs to
    buf: list[str] = []
    intro: list[str] = []
    for raw in (text or "").split("\n"):
        ln = raw.rstrip()
        if INDENT_ITEM_RE.match(raw):
            p.problems.append(f"indented item line ({ln.strip()!r}): items no "
                              "longer nest; use a group")
            continue
        el: PItem | PGroup | None = None
        gm = GROUP_HEAD_RE.match(ln)
        im = ITEM_RE.match(ln)
        if gm:
            letter = gm.group(1).upper() if gm.group(1) else None
            title = gm.group(2).strip()
            if not title:
                p.problems.append(f"group heading needs a name ({ln!r})")
            elif any(g.letter == letter for g in p.groups if letter):
                p.problems.append(f"group {letter} appears twice")
            else:
                g = PGroup(letter=letter, title=title)
                p.groups.append(g)
                cur_group = g
                el = g
        elif im:
            rest = im.group(2)
            it = PItem(no=None, checked=im.group(1) in "xX", line=ln)
            dm = DUE_RE.search(rest)
            if dm:
                it.due = dm.group(1)
                rest = rest[:dm.start()].rstrip()
            om = OWNER_RE.search(rest)
            if om:
                it.owner = om.group(1)
                rest = rest[:om.start()].rstrip()
            if NOTE_SEP in rest:
                idx = rest.index(NOTE_SEP)
                it.note = rest[idx + len(NOTE_SEP):].strip()
                rest = rest[:idx].rstrip()
            tm = TASK_REF_RE.search(rest)  # K3: strip the `→ T-xxxxxx` ref
            if tm:
                it.task = tm.group(1)
                rest = rest[:tm.start()].rstrip()
            if rest.count(" → T-") > (1 if it.task else 0):
                p.problems.append(
                    f"line {ln!r}: an item references at most one task "
                    f"(use related for more, or write the ids in the text)")
            nm = NO_RE.match(rest)
            if nm:
                try:
                    it.no = norm_no(nm.group(1))
                    rest = nm.group(2)
                except ItemsError as e:
                    p.problems.append(f"line {ln!r}: {e}")
            it.text = rest.strip()
            if not it.text:
                p.problems.append(f"empty item line ({ln!r})")
            el = it
            (cur_group.items if cur_group is not None else p.ungrouped).append(it)
        if el is not None:
            if cur is not None:
                _trailer(cur, buf)
            buf = []
            cur = el
            continue
        s = ln.strip()
        if s and cur is not None:
            buf.append(s)
        elif s and cur is None and el is None:
            intro.append(s)
    if cur is not None:
        _trailer(cur, buf)
    p.intro = "\n".join(intro)
    for it in _all_items(p):
        _resolve_state(it)
        if it.from_emoji and not it.checked:
            p.problems.append(
                f"line {it.line!r}: the emoji says {it.state} (closed) but the "
                f"checkbox is unchecked")
    return p


# ---- state transforms (pure; plans/items-design.md §5) ----------------------

def _clone(state: ItemsState) -> ItemsState:
    return copy.deepcopy(state)


def _validate_item(item: Item, spec: ItemsSpec) -> None:
    if item.is_group or item.deleted:
        return
    if not item.text.strip():
        raise ItemsError(f"item #{item.no} needs text")
    if NOTE_SEP in item.text or " · " in item.text:
        raise ItemsError(f"item #{item.no} text must not contain {NOTE_SEP!r} "
                         "or ' · ' (put them in the note/owner/due)")
    if item.owner and "owner" not in spec.fields:
        raise ItemsError("owner is not an item field of this checklist")
    if item.due and "due" not in spec.fields:
        raise ItemsError("due is not an item field of this checklist")
    if item.due:
        try:
            date.fromisoformat(item.due)
        except ValueError:
            raise ItemsError(
                f"due {item.due!r} is not a valid date (YYYY-MM-DD)") from None


def _live_no(state: ItemsState, no: str) -> Item:
    it = state.by_no(no)
    if it is None:
        raise ItemsError(f"#{no} is not an item of this checklist")
    if it.deleted:
        raise ItemsError(f"#{no} was deleted (numbers are never reused) — "
                         f"drop the number to add a new item")
    return it


def _insert_pos(new: ItemsState, letter: str, first: bool) -> int:
    """The index where an item enters a group: top (first) or bottom. A
    bottom insert goes after the group's last item, or right after its X00
    row when the group is still empty."""
    members = [i for i, r in enumerate(new.rows)
               if not r.deleted and not r.is_group
               and group_of(r.no) == letter]
    if letter:
        g = next(i for i, r in enumerate(new.rows)
                 if r.no == letter + "00")
        return members[-1] + 1 if members and not first else g + 1
    if first or not members:
        return next((i for i, r in enumerate(new.rows)
                     if r.is_group or group_of(r.no)), len(new.rows))
    return members[-1] + 1


def add(state: ItemsState, spec, text: str, *, group: str | None = None,
        after: str | None = None, state_name: str = "", note: str = "",
        owner: str = "", due: str = "", task: str = "") -> tuple[ItemsState, list[str]]:
    """Append to the end of the target group (or right after `after`, which
    must be in the same group). Returns (new_state, notes)."""
    spec = _spec(spec)
    new = _clone(state)
    letter = norm_group(group) if group else ""
    if letter:
        g = new.group(letter)
        if g is None or g.deleted:
            raise ItemsError(f"unknown group {letter}")
    a: Item | None = None
    if after:
        a = _live_no(new, norm_no(after))
        if group_of(a.no) != letter:
            raise ItemsError(f"#{a.no} is in group {group_of(a.no) or '∅'} — "
                             f"an --after item must share the target group")
    if state_name and state_name not in ITEM_STATES:
        raise ItemsError(f"state {state_name!r} is not a valid item state "
                         f"({', '.join(ITEM_STATES)})")
    if task and not task.startswith("T-"):
        raise ItemsError(f"task ref {task!r} must be a task id (T-…)")
    it = _new_item(new.next_no(letter), text.strip(),
                   state=state_name or OPEN, note=note,
                   owner=owner, due=due, task=task)
    _validate_item(it, spec)
    if a is not None:
        new.rows.insert(new.rows.index(a) + 1, it)
    else:
        new.rows.insert(_insert_pos(new, letter, first=False), it)
    return new, [f"new item #{it.no} ({it.text})"]


def _valid_state(name: str) -> None:
    if name and name not in ITEM_STATES:
        raise ItemsError(f"state {name!r} is not a valid item state "
                         f"({', '.join(ITEM_STATES)})")


def set_state(state: ItemsState, spec, no: str, state_name: str,
              note: str | None = None, commit: str = "") -> tuple[ItemsState, list[str]]:
    """Change an item's state. A no-op (same state, note=None, no commit)
    yields an unchanged state and NO notes (no empty changelog line)."""
    spec = _spec(spec)
    no = norm_no(no)
    new = _clone(state)
    it = _live_no(new, no)
    _valid_state(state_name)
    if it.state == state_name and note is None and not commit:
        return new, []
    it.state = state_name
    if note is not None:
        it.note = note
    if commit:
        it.commit = commit
    _validate_item(it, spec)
    was_closed = it.state in CLOSED_STATES
    it.closed_at = dbmod.utc_now() if was_closed else ""
    # a note only when the item moves to a closed state (open = uncheck: silent)
    notes = [f"item #{no} {state_name}"] if was_closed else []
    if note is not None or commit:
        notes.append(f"item #{no} edited")
    return new, notes


def edit(state: ItemsState, spec, no: str, *, text: str | None = None,
         note: str | None = None, owner: str | None = None,
         due: str | None = None, task: str | None = None) -> tuple[ItemsState, list[str]]:
    """Edit fields; '' clears a field (incl. the task ref, K3). No change → no notes."""
    spec = _spec(spec)
    no = norm_no(no)
    new = _clone(state)
    it = _live_no(new, no)
    changed = False
    for name, val in (("text", text), ("note", note),
                      ("owner", owner), ("due", due), ("task", task)):
        if val is None:
            continue
        cur = val.strip() if name == "text" else val
        if getattr(it, name) != cur:
            setattr(it, name, cur)
            changed = True
    _validate_item(it, spec)
    return new, ([f"item #{no} edited"] if changed else [])


def move(state: ItemsState, spec, no: str, *, group: str | None = None,
         ungroup: bool = False, after: str | None = None,
         first: bool = False) -> tuple[ItemsState, list[str]]:
    """Move an item: cross-group moves get a FRESH number (the old one becomes
    a tombstone); within a group only the order changes. Exactly one of
    group / ungroup / after / first."""
    spec = _spec(spec)
    no = norm_no(no)
    n = sum((bool(group), ungroup, bool(after), first))
    if n != 1:
        raise ItemsError("exactly one of --group / --ungroup / --after / --first")
    new = _clone(state)
    it = _live_no(new, no)
    old_grp = group_of(no)
    if group:
        target = norm_group(group)
        g = new.group(target)
        if g is None or g.deleted:
            raise ItemsError(f"unknown group {target}")
    else:
        target = "" if ungroup else old_grp
    new.rows.remove(it)
    note = ""
    if target != old_grp:
        fresh = new.next_no(target)
        new.rows.append(_tomb(no, it.text))
        it.no = fresh
        note = _n_moved(no, fresh)
    if after:
        a = _live_no(new, norm_no(after))
        if group_of(a.no) != target:
            raise ItemsError(f"#{a.no} is in group {group_of(a.no) or '∅'} — "
                             f"an --after item must share the target group")
        new.rows.insert(new.rows.index(a) + 1, it)
    else:
        new.rows.insert(_insert_pos(new, target, first=bool(first)), it)
    _validate_item(it, spec)
    return new, ([note] if note else [])


def remove(state: ItemsState, spec, no: str) -> tuple[ItemsState, list[str]]:
    """Mark a row a tombstone (the number is never reused)."""
    spec = _spec(spec)
    no = norm_no(no)
    new = _clone(state)
    it = _live_no(new, no)
    it.deleted = True
    new.rows.remove(it)
    new.rows.append(it)
    return new, []


# one wording for group changes — the command/sync notes and the changelog
def _n_group_added(letter: str, title: str) -> str:
    return f"group {letter} added: {title}"


def _n_group_renamed(letter: str, old: str, title: str) -> str:
    return f"group {letter} renamed: {old} → {title}"


def _n_group_removed(letter: str, title: str) -> str:
    return f"group {letter} removed: {title}"


def _n_moved(old: str, fresh: str) -> str:
    """A renumbering move; diff_log/sync_section recognise exactly this shape."""
    return f"item #{old} → #{fresh}"


def _moves(notes: list[str]) -> list[tuple[str, str]]:
    """(old, fresh) for every `_n_moved` note."""
    out = []
    for x in notes:
        m = re.fullmatch(r"item #(\w+) → #(\w+)", x)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def group_add(state: ItemsState, spec, title: str) -> tuple[ItemsState, list[str]]:
    """A new group: the next free letter, an `X00` row appended at the end."""
    spec = _spec(spec)
    title = title.strip()
    if not title:
        raise ItemsError("a group needs a name")
    new = _clone(state)
    letter = new.next_group()
    row = _new_item(letter + "00", text=title)
    # before any tombstones (they must stay last)
    pos = next((i for i, r in enumerate(new.rows) if r.deleted), len(new.rows))
    new.rows.insert(pos, row)
    return new, [_n_group_added(letter, title)]


def group_rename(state: ItemsState, spec, letter: str, title: str
                 ) -> tuple[ItemsState, list[str]]:
    """Rename a group (its letter is unchanged)."""
    spec = _spec(spec)
    letter = norm_group(letter)
    new = _clone(state)
    g = _live_no(new, letter + "00")
    title = title.strip()
    if g.text == title:
        return new, []
    old, g.text = g.text, title
    return new, [_n_group_renamed(letter, old, title)]


def group_mv(state: ItemsState, spec, letter: str, *, after: str | None = None,
             first: bool = False) -> tuple[ItemsState, list[str]]:
    """Move a group (its whole block: `X00` + items): exactly one of
    after / first."""
    spec = _spec(spec)
    letter = norm_group(letter)
    if sum((bool(after), first)) != 1:
        raise ItemsError("exactly one of --after / --first")
    new = _clone(state)
    g = _live_no(new, letter + "00")
    block = [r for r in new.rows
             if r is g or (not r.deleted and not r.is_group
                           and group_of(r.no) == letter)]
    for r in block:
        new.rows.remove(r)
    if first:
        pos = _insert_pos(new, "", first=True)
    else:
        a = _live_no(new, norm_no(after))
        pos = new.rows.index(a) + 1
    for r in block:
        new.rows.insert(pos, r)
        pos += 1
    return new, []


def group_rm(state: ItemsState, spec, letter: str,
             items: str | None = None) -> tuple[ItemsState, list[str]]:
    """Delete a group: the `X00` row becomes a tombstone (the letter is never
    reused). A non-empty group needs items='ungroup' (the items move to the
    ungrouped list with fresh numbers) or 'delete'."""
    spec = _spec(spec)
    letter = norm_group(letter)
    new = _clone(state)
    g = _live_no(new, letter + "00")
    members = [r for r in new.rows if not r.deleted and not r.is_group
               and group_of(r.no) == letter]
    notes: list[str] = []
    if members:
        if items not in ("ungroup", "delete"):
            raise ItemsError(f"group {letter} still has {len(members)} items — "
                             f"pass items='ungroup' or 'delete'")
        if items == "delete":
            for m in members:
                m.deleted = True
                new.rows.remove(m)
                new.rows.append(m)
        else:
            # same as any cross-group move: a fresh number, the old one a tombstone
            for m in sorted(members, key=lambda r: r.no):
                new.rows.remove(m)
                new.rows.append(_tomb(m.no, m.text))
                fresh = new.next_no("")
                notes.append(_n_moved(m.no, fresh))
                m.no = fresh
                new.rows.insert(_insert_pos(new, "", first=False), m)
                _validate_item(m, spec)
    g.deleted = True
    new.rows.remove(g)
    new.rows.append(g)
    return new, [*notes, _n_group_removed(letter, g.text)]


# ---- validate / diff (shared by sync and the commands) ----------------------

def validate(old: ItemsState, new: ItemsState, spec,
             record_status: str, gates_items: list[str] | None = None) -> None:
    spec = _spec(spec)
    caps: dict[str, int] = {}
    for r in new.live():
        if r.is_group:
            continue
        g = group_of(r.no)
        caps[g] = caps.get(g, 0) + 1
        if caps[g] > (GROUP_CAP if g else UNGROUPED_CAP):
            raise ItemsError(f"group {g or '∅'} exceeds its capacity "
                             f"({GROUP_CAP if g else UNGROUPED_CAP})")
    if len([r for r in new.live() if r.is_group]) > len(LETTERS):
        raise ItemsError(f"at most {len(LETTERS)} groups")
    if record_status in (gates_items or []):
        open_n = open_items(new, spec)
        if open_n:
            raise ItemsError(
                f"the {record_status} state requires all items closed: "
                + ", ".join(open_n))
    new.check()


def diff_log(old: ItemsState, new: ItemsState, notes: list[str]
             ) -> list[tuple[str, str]]:
    """(category, message) lines by comparing the two states by number.
    Order-only changes produce nothing. `notes` carries the cross-group move
    renumberings ('item #003 → #A04') made by the transforms — a move writes
    ONE Changed line (the fresh number's 'Added' is suppressed)."""
    moves = _moves(notes)
    moved_from = {a for a, _ in moves}
    moved_to = {b for _, b in moves}
    o = {r.no: r for r in old.rows}
    n = {r.no: r for r in new.rows}
    out: list[tuple[str, str]] = []
    for no, r in n.items():
        if no not in o:
            if r.is_group:
                out.append(("Added", f"group {no[0]}: {r.text}"))
            elif not r.deleted and no not in moved_to:
                out.append(("Added", f"item #{no}: {r.text}"))
            elif r.deleted and no not in moved_to:
                out.append(("Removed", f"item #{no}: {r.text}"))
    for no, r in o.items():
        if r.is_group or r.deleted or no in moved_from:
            continue
        nr = n.get(no)
        if nr is None or nr.deleted:
            out.append(("Removed", f"item #{no}: {r.text}"))
    for no, r in o.items():
        if no in n and r.is_group:
            if n[no].deleted and not r.deleted:
                out.append(("Removed", f"group {no[0]}: {r.text}"))
            elif not n[no].deleted and n[no].text != r.text:
                out.append(("Changed", _n_group_renamed(no[0], r.text, n[no].text)))
    for no, r in n.items():
        if (no in o and not r.is_group and not r.deleted
                and no not in moved_to and o[no] is not None
                and not o[no].deleted):
            a = o[no]
            if r.state != a.state:
                out.append(("Changed", f"item #{no} {a.state} → {r.state}"))
            elif any(getattr(r, f) != getattr(a, f) for f in
                     ("text", "note", "owner", "due", "commit", "trailer")):
                out.append(("Changed", f"item #{no} edited"))
    for a, b in moves:
        out.append(("Changed", _n_moved(a, b)))
    return out


# ---- the one write path ------------------------------------------------------

def progress(state: ItemsState, spec) -> tuple[int, int]:
    """(closed, total) — the n/m of a task row (all groups counted).
    `spec`: the ItemsSpec or its RecordTypeSpec."""
    items = [i for i in state.live() if not i.is_group]
    closed = sum(1 for i in items if i.state in CLOSED_STATES)
    return closed, len(items)


def open_items(state: ItemsState, spec) -> list[str]:
    """The unclosed items as '#no text' — the gates.items message.
    `spec`: the ItemsSpec or its RecordTypeSpec."""
    return [f"#{i.no} {i.text}" for i in state.live()
            if not i.is_group and i.state not in CLOSED_STATES]


def load(db, record_id: str, spec) -> ItemsState:
    """One checklist (its `##` section) from the DB: its rows, plus
    `occupied` = the numbers used by the record's other checklists (numbering
    is shared record-wide). The intro is set by the caller (records.sections)."""
    itspec = _spec(spec)
    state = ItemsState()
    for r in db.items_load(record_id):
        if r.get("section") == itspec.section:
            state.rows.append(Item(no=r["no"], text=r["text"], state=r["state"],
                                   note=r["note"], owner=r["owner"], due=r["due"],
                                   commit=r["commit"], trailer=r["trailer"],
                                   created=r["created"], closed_at=r["closed_at"],
                                   deleted=bool(r["deleted"]),
                                   section=itspec.section,
                                   task=r.get("task", "")))
        else:
            state.occupied.append(r["no"])
    return state


def commit(db, spec, row: dict, old: ItemsState, new: ItemsState,
           notes: list[str], it=None) -> list[str]:
    """The single write path: validate → diff_log → items_save → add_log →
    (auto-promote). `spec`: the RecordTypeSpec (for the record-level writes);
    `it`: the checklist being written (its gate/promote) — default the primary
    one. Returns the notes."""
    itspec = it if it is not None else _spec(spec)
    validate(old, new, itspec, row["status"], itspec.gate)
    _check_task_refs_closed(db, old, new, itspec)  # P3 reverse gate
    for cat, msg in diff_log(old, new, notes):
        db.add_log(row["id"], msg, category=cat)
    # P1: stamp this checklist's section on rows that don't carry it yet (fresh
    # items / tombstones / group rows are built without one); items_save then
    # rewrites the WHOLE record, so the other checklists' rows (untouched here)
    # must go back in too, or they vanish from the table.
    for r in new.rows:
        if not r.section:
            r.section = itspec.section
    others = [r for r in db.items_load(row["id"])
              if r.get("section") != itspec.section]
    db.items_save(row["id"], [r.to_row() for r in new.rows] + others)
    _maybe_promote(db, spec, row, new.live(), notes, itspec)
    return list(notes)


def _maybe_promote(db, spec, row: dict, live, notes: list, itspec) -> None:
    """The record's checklist promote (PLAN §10.3b): entering promote_to as
    soon as the record is in promote_from and at least one item is closed.
    Shared by commit (a hand edit) and P3's task→item sync (an auto close)."""
    if not (itspec.promote_to and itspec.promote_from
            and row["status"] in itspec.promote_from
            and any(not i.is_group and i.state in itspec.closed_states
                    for i in live)):
        return
    old_status = row["status"]
    db.set(spec, row["id"], status=itspec.promote_to)
    db.add_log(row["id"],
               f"promoted {old_status} → {itspec.promote_to}",
               category="Changed")
    notes.append(f"{row['id']} promoted {old_status} → {itspec.promote_to}")
    # W3: the promoted status may be the record's wip.enter (a task's first
    # closed item promotes Draft -> In Progress) - the automatic WIP entry
    # rides this switch exactly like a manual one (status_set).
    from proj.engine import wip as wipmod  # lazy: items<->wip cycle
    wipmod.auto_status_wip(db, spec, row["id"], old_status, itspec.promote_to)


def _check_task_refs_closed(db, old: ItemsState, new: ItemsState, itspec) -> None:
    """P3 reverse gate: manually CLOSING an item (into a closed state) whose
    `→ task` reference still points at a NON-TERMINAL task is refused — the
    task owns that item's closure (a terminal task closes it, §10.3 K3).
    Editing an already-closed item, and closing items with no reference (or a
    terminal one), are fine."""
    old_state = {i.no: i.state for i in old.live()}
    newly_closed = [i for i in new.live()
                    if not i.is_group and i.task
                    and i.state in itspec.closed_states
                    and old_state.get(i.no, OPEN) not in itspec.closed_states]
    if not newly_closed:
        return
    from proj.engine import store as rec  # lazy: items<->store cycle
    tspec = rec.load_registry(Path(db.path).parent)["task"]
    for i in newly_closed:
        trow = db.conn.execute(
            "SELECT status FROM records WHERE id=?", (i.task,)).fetchone()
        if trow is None:
            raise ItemsError(
                f"#{i.no} references {i.task} which does not exist any more — "
                f"clear the ref first (item edit … --task '')")
        if trow["status"] not in tspec.status_terminal:
            raise ItemsError(
                f"#{i.no} references {i.task} which is still {trow['status']} — "
                f"finish the task (it closes this item) or clear the ref first "
                f"(item edit … --task '')")


def sync_task_ref(db, task_spec, task_id: str, new_status: str,
                  registry=None) -> list[str]:
    """P3 forward sync: the task changed status — every LIVE checklist item
    that references it (`→ task`) follows its terminal state (PLAN §26.2):
    task Done → item done, task Failed → item failed, task moved back to an
    open status → item open. Runs inside the caller's transaction (the task
    status_set); the changelog line names the cause. An auto-CLOSE also rides
    the record's checklist promote (a referenced task closing its item can move
    the record Draft → In Progress). Returns the notes."""
    if new_status in task_spec.status_done:
        target = DONE
    elif new_status in task_spec.status_terminal:
        target = FAILED
    else:
        target = OPEN
    emoji = task_spec.status_emoji.get(new_status, "")
    cause = f"auto: {task_id} {new_status} {emoji}".rstrip()
    notes: list[str] = []
    if registry is None:
        from proj.engine import store as rec  # lazy
        registry = rec.load_registry(Path(db.path).parent)
    changed_records: dict[str, list[str]] = {}
    for r in db.conn.execute(
            "SELECT record_id, no, section, state FROM items "
            "WHERE task=? AND deleted=0", (task_id,)):
        if r["state"] == target:
            continue
        db.conn.execute(
            "UPDATE items SET state=?, closed_at=? WHERE record_id=? AND no=?",
            (target, dbmod.utc_now() if target in CLOSED_STATES else "",
             r["record_id"], r["no"]))
        db.add_log(r["record_id"],
                   f"item #{r['no']} {r['state']} → {target} ({cause})",
                   category="Changed")
        notes.append(f"{r['record_id']}: item #{r['no']} → {target} ({cause})")
        changed_records.setdefault(r["record_id"], []).append(r["section"])
    # promote: a record whose item just auto-CLOSED may hit its promote trigger
    if target in CLOSED_STATES:
        for record_id, sections in changed_records.items():
            rspec = registry[db.conn.execute(
                "SELECT type FROM records WHERE id=?",
                (record_id,)).fetchone()["type"]]
            for section in dict.fromkeys(sections):
                itspec = next(it for it in rspec.items if it.section == section)
                frow = db.get(rspec, record_id)
                state = load(db, record_id, itspec)
                _maybe_promote(db, rspec, frow, state.live(), notes, itspec)
    return notes


def sync_section(db, spec, row: dict, md_text: str,
                 source: str = "", adopt: bool = False,
                 it=None) -> tuple[list[str], list[str]]:
    """Store's sync entry: parse → apply_md → commit. On problems nothing is
    written and the md stays as-is. `spec`: the RecordTypeSpec; `it`: the
    checklist this md section belongs to (default the primary one). `source`:
    the filename each note is prefixed with. adopt=True: every line is treated
    as a NEW item (the stored record is fresh, so no number can refer to it —
    the numbers are cleared). The notes reported to the user are the
    sync-level ones (new/deleted items plus moves/emoji/promote from commit),
    each prefixed with source."""
    itspec = it if it is not None else _spec(spec)
    parsed = parse_section(md_text, itspec)
    if adopt:
        for it in _all_items(parsed):
            it.no = None
    if parsed.problems:
        return parsed.problems, []
    old = load(db, row["id"], spec)
    old.intro = _intro_of(spec, row)
    old_nos = {i.no for i in old.live()}
    try:
        new, notes = apply_md(old, parsed, itspec)
        notes = commit(db, spec, row, old, new, notes, it=itspec)
    except ItemsError as e:
        return [str(e)], []
    new_nos = {i.no for i in new.live()}
    # items only (groups have their own notes); a move is ONE "#a → #b" note,
    # never also "new #b" + "#a deleted"
    moves = _moves(notes)
    moved_from = {a for a, _ in moves}
    moved_to = {b for _, b in moves}
    sync_notes: list[str] = [f"new item #{i.no} ({i.text})" for i in new.live()
                             if not i.is_group and i.no not in old_nos
                             and i.no not in moved_to]
    sync_notes += [f"item #{i.no} deleted ({i.text})" for i in old.live()
                   if not i.is_group and i.no not in new_nos
                   and i.no not in moved_from]
    sync_notes += notes  # groups, moves, emoji defaults, promote (from commit)
    db.set(spec, row["id"],
           sections=_set_intro(itspec, row, new.intro)["sections"])  # already dumped
    return [], [f"{source}: {n}" for n in sync_notes]


def drift(db, spec, row: dict, md_text: str) -> bool:
    """normalize(md_text) != render_section(load(...)) — an unsynced edit."""
    state = load(db, row["id"], spec)
    state.intro = _intro_of(spec, row)
    return normalize(md_text) != render_section(state, spec)


# ---- apply_md (sync: merge the parsed md into the state) ---------------------

def apply_md(state: ItemsState, parsed: Parsed, spec
             ) -> tuple[ItemsState, list[str]]:
    """Merge a Parsed section into the state (plans/items-design.md §5.1):
    known numbers move (cross-group → fresh number, the old one a tombstone),
    unnamed headings get a letter, missing rows become tombstones."""
    spec = _spec(spec)
    if parsed.problems:
        raise ItemsError("; ".join(parsed.problems))
    new = _clone(state)
    notes = list(parsed.notes)
    # 1. groups
    for g in parsed.groups:
        if g.letter:
            row = new.group(g.letter)
            if row is None or row.deleted:
                raise ItemsError(f"unknown group {g.letter} — the letter was "
                                 f"never assigned")
            if row.text != g.title:
                old_title, row.text = row.text, g.title
                notes.append(_n_group_renamed(g.letter, old_title, g.title))
        else:
            letter = new.next_group()
            g.letter = letter
            new.rows.append(_new_item(letter + "00", text=g.title))
            notes.append(_n_group_added(letter, g.title))
    named = {g.letter for g in parsed.groups if g.letter}
    for r in [r for r in new.rows if r.is_group and not r.deleted
              and r.no[0] not in named]:
        r.deleted = True
        new.rows.remove(r)
        new.rows.append(r)
        notes.append(_n_group_removed(r.no[0], r.text))
    # 2. items (md order: ungrouped first, then groups in heading order)
    ordered: list[tuple[PItem, str]] = [(i, "") for i in parsed.ungrouped]
    ordered += [(i, g.letter) for g in parsed.groups for i in g.items]
    md_nos: set[str] = set()
    live: list[Item] = []
    if parsed.intro.strip():
        new.intro = parsed.intro
    for it, grp in ordered:
        if it.no is None:
            row = _new_item(new.next_no(grp), it.text,
                            state=it.state or OPEN,
                            note=it.note, owner=it.owner, due=it.due,
                            trailer=it.trailer)
            _validate_item(row, spec)
            md_nos.add(row.no)
            live.append(row)
            new.rows.append(row)  # so the next fresh number sees this one
            continue  # the "new item" note is added by sync_section (with source)
        if it.no in md_nos:
            raise ItemsError(f"duplicate item number #{it.no} — drop the number "
                             f"to add it as new")
        row = new.by_no(it.no)
        if row is None:
            raise ItemsError(f"unknown item number #{it.no} — drop the number "
                             f"to add it as new")
        md_nos.add(it.no)
        new.rows.remove(row)
        if group_of(it.no) != grp:
            new.rows.append(_tomb(it.no, row.text))
            fresh = new.next_no(grp)
            row.no = fresh
            notes.append(_n_moved(it.no, fresh))
        new.rows.append(row)
        row.text, row.note = it.text, it.note
        row.owner, row.due = it.owner, it.due
        row.task = it.task  # K3: the → task reference
        row.trailer = it.trailer
        if it.state and row.state != it.state:
            row.state = it.state
            row.closed_at = dbmod.utc_now() if it.state in CLOSED_STATES \
                else ""
        _validate_item(row, spec)
        live.append(row)
    # 3. missing items → tombstones. "Kept" is decided by identity, not by
    # number: a cross-group move renumbered its row (md_nos holds the OLD one).
    kept = {id(r) for r in live}
    for r in [r for r in new.rows if not r.deleted and not r.is_group
              and id(r) not in kept]:
        r.deleted = True  # the "deleted" note is added by sync_section
    # 4. display order: ungrouped, then each group block (X00 + its items),
    # tombstones last. Deduped by object identity (a row is referenced both by
    # `live` and still present in new.rows for the next_no lookups).
    blocks: list[Item] = [r for r in live if not group_of(r.no)]
    for g in parsed.groups:
        blocks.append(new.group(g.letter))
        blocks.extend(r for r in live if group_of(r.no) == g.letter)
    tombstones = [r for r in new.rows if r.deleted]
    seen: set[int] = set()
    out: list[Item] = []
    for r in [*blocks, *tombstones]:
        if id(r) in seen:
            continue
        seen.add(id(r))
        out.append(r)
    new.rows = out
    new.check()
    return new, notes
