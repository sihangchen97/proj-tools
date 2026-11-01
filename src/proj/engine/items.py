"""Item checklists — pure model, md parse/render, state transforms and DB
access (depends only on proj.core, NOT on store — see plans/items-design.md).
CLI/MCP operations live in engine/item_ops.py (which may depend on store).

The `items:` block of a record template defines one structured `##` section.
This module is the single owner of its rules:

- ITEM NUMBER: 3 chars — `001`…`999` (ungrouped, max 999) or `L01`…`L99`
  (grouped: L = group letter A-Z, max 99 per group, max 26 groups).
  Numbers are NEVER reused (one counter per group); group letters are never
  reused either. Parsing accepts zero-shortened refs (`#1` = `#001`,
  `#a1` = `#A01`, case-insensitive); rendering is always the full 3-char form.
  (The LETTER in a RECORD id means nothing — record ids are digits only.)
- MD SHAPE: a free mix of free text, group headings and item lines.
  A group heading is `### {LETTER} · {name}` (letter assigned by the tool;
  a heading without a letter creates a new group). An item belongs to the
  NEAREST heading above it; items before the first heading are ungrouped.
  Item lines are top-level only (an indented `- [ ]` is a problem — use a
  group). One item line:
  `- [x | ] #{no} {emoji + " " (only when the state's closed-group holds >1
  state)} {text}{" — " + note}{" · owner: " + owner}{" · due: " + due}`.
  Parse strips right-to-left (due → owner → first " — " → #number → emoji).
- SYNC (md → DB, `apply_md`): everything is renumbered from the md; a line
  with a number the record has not issued is a problem (drop the number to
  add it as new); duplicate numbers are a problem; a numbered line sitting
  under a DIFFERENT heading than its stored group is a cross-group move —
  it gets a FRESH number (changelog `item #003 → #A04`, old number void);
  a stored item missing from the md is deleted; a heading that disappears
  deletes its group (its items are re-homed per the rules above); empty
  groups are kept; free text is kept verbatim (intro + trailers).
- RE-OPEN RESTRICTION: on a record in a `gates.items` status, no unclosed
  item may survive the write.

All transforms are pure: `(state, spec, ...) -> (new_state, notes)`, raising
`ItemsError` (a ValueError) on any violation and never mutating their input.
`commit` is the single write path: validate → diff_log → items_save →
add_log → promote. Sync (`sync_section`) and every command share it, so both
produce identical changelogs.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import date

from proj.core import db as dbmod
from proj.core.templates import ItemsSpec, RecordTypeSpec

# ---- constants ---------------------------------------------------------------

UNGROUPED = ""
UNGROUPED_CAP = 999
GROUP_CAP = 99
GROUP_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NOTE_SEP = " — "

GROUP_HEADING_RE = re.compile(r"^### (?:([A-Za-z]) · )?(.+)$")
ITEM_LINE_RE = re.compile(r"^( *)- \[([ xX])\] (.*)$")
ITEM_NO_RE = re.compile(r"^#(\w+) ")
DUE_RE = re.compile(r" · due: (\d{4}-\d{2}-\d{2})$")
OWNER_RE = re.compile(r" · owner: (.+)$")
MOVE_NOTE_RE = re.compile(r"^(#\S+) → (\S+)$")


class ItemsError(ValueError):
    """A rejected item operation (parse problem, capacity, unknown number…)."""


# ---- model (plans/items-design.md §3) ----------------------------------------

@dataclass(frozen=True)
class ItemNo:
    """The 3-char item number: `001` (ungrouped) / `A01` (group A)."""

    grp: str
    seq: int

    def __str__(self) -> str:
        return f"{self.seq:03d}" if not self.grp else f"{self.grp}{self.seq:02d}"


def parse_no(s: str) -> ItemNo:
    """'1' / '001' / 'a1' / 'A01' → ItemNo (case-insensitive, zero-shortened);
    ValueError on a malformed or out-of-range number."""
    m = re.match(r"^([A-Za-z])?(\d{1,3})$", s.strip())
    if not m:
        raise ValueError(f"malformed item number {s!r} (expected e.g. 001 or A01)")
    letter = (m.group(1) or "").upper()
    seq = int(m.group(2))
    if letter and seq > GROUP_CAP:
        raise ValueError(f"item number #{letter}{seq} is out of range "
                         f"(group items run to #{letter}{GROUP_CAP:02d})")
    if not letter and seq > UNGROUPED_CAP:
        raise ValueError(f"item number #{seq} is out of range "
                         f"(ungrouped items run to #{UNGROUPED_CAP:03d})")
    return ItemNo(letter, seq)


@dataclass
class Item:
    no: ItemNo
    text: str
    state: str
    note: str = ""
    owner: str = ""
    due: str = ""
    created: str = ""
    closed_at: str = ""
    commit: str = ""
    trailer: str = ""      # free text directly under this line (kept verbatim)


@dataclass
class Group:
    letter: str
    title: str
    trailer: str = ""      # free text under the heading, before the first item


@dataclass
class ItemsState:
    """A record's full checklist state. `items` order = display order."""

    intro: str
    items: list[Item] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    last_seq: dict[str, int] = field(default_factory=dict)   # per-group counter
    last_group: int = 0                                      # groups allocated

    def check(self) -> None:
        """Invariants (asserted before every save):
        - items: all ungrouped first, then the groups' items in groups' order;
        - item numbers unique; every seq <= the group's counter;
        - group letters unique and within the allocated range."""
        seen: set[ItemNo] = set()
        ordered: list[Item] = [i for i in self.items if not i.no.grp]
        for g in self.groups:
            ordered += [i for i in self.items if i.no.grp == g.letter]
        if ordered != list(self.items):
            raise AssertionError("items order invariant broken (ungrouped first, groups in order)")
        for i in self.items:
            if i.no in seen:
                raise AssertionError(f"duplicate item number #{i.no}")
            seen.add(i.no)
            if i.no.seq > self.last_seq.get(i.no.grp, 0):
                raise AssertionError(f"item #{i.no} exceeds the group counter")
        letters = [g.letter for g in self.groups]
        if len(letters) != len(set(letters)):
            raise AssertionError("duplicate group letter")
        for g in self.groups:
            if GROUP_LETTERS.index(g.letter) + 1 > self.last_group:
                raise AssertionError(f"group letter {g.letter} beyond the allocated range")


# ---- normalize (same rule as store.normalize — items.py must not import store)

def normalize(text: str) -> str:
    """Drop leading/trailing blank lines; compress runs of 2+ blank lines to 1
    (outside code fences). Content as-is."""
    out: list[str] = []
    fence, blank = False, 0
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


# ---- spec helpers ------------------------------------------------------------

def _state_of(spec: ItemsSpec, name: str):
    return next((s for s in spec.states if s.name == name), None)


def _state_name(spec: ItemsSpec, name: str | None) -> str:
    if name is None:
        return spec.states[0].name
    if _state_of(spec, name) is None:
        raise ItemsError(f"unknown state {name!r} (expected: "
                         f"{', '.join(s.name for s in spec.states)})")
    return name


def _show_emoji(spec: ItemsSpec, state_name: str) -> str:
    """The state's emoji + trailing space, only when its closed-group holds >1 state."""
    s = _state_of(spec, state_name)
    if s is None or not s.emoji:
        return ""
    if len([x for x in spec.states if x.closed == s.closed]) > 1:
        return f"{s.emoji} "
    return ""


def _capacity(grp: str, seq: int) -> None:
    if grp and seq > GROUP_CAP:
        raise ItemsError(f"group {grp} is full ({GROUP_CAP} items) — split the record")
    if not grp and seq > UNGROUPED_CAP:
        raise ItemsError(f"the ungrouped list is full ({UNGROUPED_CAP} items) — split the record")


def _by_no(state: ItemsState, no: ItemNo) -> Item:
    for i in state.items:
        if i.no == no:
            return i
    raise ItemsError(f"item #{no} not found")


def _by_letter(state: ItemsState, letter: str) -> Group:
    for g in state.groups:
        if g.letter == letter:
            return g
    raise ItemsError(f"group {letter} does not exist")


def _grp_bounds(state: ItemsState, grp: str) -> tuple[int, int]:
    """(start, end) of the group's item block in the display list (blocks are
    contiguous: ungrouped first, then the groups in order — the invariant).
    For an empty group both are where its block would start (end of the
    previous block), so insert-at-end and insert-at-first coincide there."""
    pos = 0
    for b in [""] + [g.letter for g in state.groups]:
        n = sum(1 for x in state.items if x.no.grp == b)
        if b == grp:
            return pos, pos + n
        pos += n
    return pos, pos  # grp not allocated: its (empty) block is last


def _validate_item(spec: ItemsSpec, text: str, owner: str = "", due: str = "") -> None:
    if not text.strip():
        raise ItemsError("item text must be non-empty")
    if NOTE_SEP in text or " · " in text:
        raise ItemsError(f"item text may not contain {NOTE_SEP!r} or ' · ' "
                         f"(use --note / --owner / --due for those)")
    if owner and "owner" not in spec.fields:
        raise ItemsError(f"{spec.noun}s of this record type have no owner field")
    if due:
        if "due" not in spec.fields:
            raise ItemsError(f"{spec.noun}s of this record type have no due field")
        try:
            date.fromisoformat(due)
        except ValueError:
            raise ItemsError(f"due {due!r} is not a valid date (YYYY-MM-DD)") from None


# ---- render (plans/items-design.md §4.1) --------------------------------------

def _it_spec(spec) -> ItemsSpec:
    """The ItemsSpec behind a spec (a RecordTypeSpec passes its .items)."""
    return spec.items if hasattr(spec, "items") and spec.items is not None else spec


def render_item_line(item: Item, spec) -> str:
    spec = _it_spec(spec)
    box = "x" if item.state in spec.closed_states else " "
    line = f"- [{box}] #{item.no} {_show_emoji(spec, item.state)}{item.text}"
    if item.note:
        line += f"{NOTE_SEP}{item.note}"
    if item.owner:
        line += f" · owner: {item.owner}"
    if item.due:
        line += f" · due: {item.due}"
    return line


def render_section(state: ItemsState, spec) -> str:
    """The section content (without the `##` heading) — depends ONLY on state.
    `spec`: the ItemsSpec or its RecordTypeSpec."""
    spec = _it_spec(spec)
    out: list[str] = []

    def emit(g: Group) -> None:
        nonlocal out
        out.append("")
        out.append(f"### {g.letter} · {g.title}")
        if g.trailer.strip():
            out.extend(g.trailer.strip("\n").splitlines())

    if state.intro.strip():
        out += state.intro.strip("\n").splitlines()
        out.append("")
    # groups are emitted in their own order (empty ones too — they must survive
    # a re-render, so they round-trip through sync); items are in display order
    gi = 0
    cur: str | None = None
    for item in state.items:
        if item.no.grp:
            while gi < len(state.groups) and state.groups[gi].letter != item.no.grp:
                emit(state.groups[gi])  # an empty group (no items yet)
                gi += 1
            if item.no.grp != cur:
                emit(state.groups[gi])
                gi += 1
                cur = item.no.grp
        out.append(render_item_line(item, spec))
        if item.trailer.strip():
            out += item.trailer.strip("\n").splitlines()
    while gi < len(state.groups):
        emit(state.groups[gi])
        gi += 1
    return normalize("\n".join(out))


# ---- parse (plans/items-design.md §4.2) ---------------------------------------

@dataclass
class PItem:
    no: ItemNo | None
    checked: bool
    state: str
    text: str
    note: str = ""
    owner: str = ""
    due: str = ""
    trailer: str = ""
    line: str = ""


@dataclass
class PGroup:
    letter: str | None
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


def _parse_item_line(line: str, box: str, rest: str, spec: ItemsSpec,
                     parsed: Parsed) -> PItem | None:
    """One item line, content stripped right-to-left; None when malformed."""
    checked = box.lower() == "x"
    due = owner = note = ""
    dm = DUE_RE.search(rest)
    if dm:
        if "due" not in spec.fields:
            parsed.problems.append(f"item line has · due: but due is not an "
                                   f"enabled field: {line!r}")
        due, rest = dm.group(1), rest[:dm.start()]
    om = OWNER_RE.search(rest)
    if om:
        if "owner" not in spec.fields:
            parsed.problems.append(f"item line has · owner: but owner is not "
                                   f"an enabled field: {line!r}")
        owner, rest = om.group(1), rest[:om.start()]
    if NOTE_SEP in rest:
        rest, note = rest.split(NOTE_SEP, 1)
        note = note.strip()
    no: ItemNo | None = None
    nm = ITEM_NO_RE.match(rest)
    if nm:
        try:
            no = parse_no(nm.group(1))
        except ValueError as e:
            parsed.problems.append(f"item line {line!r}: {e}")
            return None
        rest = rest[nm.end():]
    state: str | None = None
    for s in spec.states:
        if s.emoji and rest.startswith(s.emoji + " "):
            state = s.name
            rest = rest[len(s.emoji) + 1:]
            break
    text = rest.strip()
    if not text:
        parsed.problems.append(f"item line has empty text: {line!r}")
        return None
    group = [s for s in spec.states if s.closed == checked]
    label = f"#{no}" if no is not None else "new item"
    if state is not None:
        if state not in {s.name for s in group}:
            parsed.problems.append(
                f"item {label}: emoji and checkbox conflict "
                f"({('[x]' if checked else '[ ]')} with {state!r}): {line!r}")
            return None
    else:
        state = group[0].name
        if checked and len(group) > 1:
            parsed.notes.append(f"item {label}: [x] without state emoji → {state}: {line!r}")
    return PItem(no=no, checked=checked, state=state, text=text, note=note,
                 owner=owner, due=due, trailer="", line=line)


def parse_section(text: str, spec: ItemsSpec) -> Parsed:
    """Scan the section line by line: group headings, top-level item lines and
    free text (intro before the first element, else the trailer of the element
    above). An indented `- [ ]` line is a problem (nesting is gone — groups)."""
    parsed = Parsed()
    intro: list[str] = []
    cur_item: PItem | None = None
    cur_group: PGroup | None = None

    for line in text.splitlines():
        g = GROUP_HEADING_RE.match(line)
        if g:
            letter = (g.group(1) or "").upper() or None
            cur_group = PGroup(letter=letter, title=g.group(2).strip())
            parsed.groups.append(cur_group)
            cur_item = None
            continue
        m = ITEM_LINE_RE.match(line)
        if m:
            if m.group(1):
                parsed.problems.append(
                    f"item line is indented — items no longer nest; use a group "
                    f"(`### L · name`) instead: {line!r}")
                cur_item = None
                continue
            p = _parse_item_line(line, m.group(2), m.group(3), spec, parsed)
            if p is not None:
                (cur_group.items if cur_group is not None else parsed.ungrouped).append(p)
                cur_item = p
            continue
        # free text
        if cur_item is not None:
            cur_item.trailer = (cur_item.trailer + "\n" + line) if cur_item.trailer else line
        elif cur_group is not None:
            cur_group.trailer = (cur_group.trailer + "\n" + line) if cur_group.trailer else line
        else:
            intro.append(line)
    parsed.intro = normalize("\n".join(intro))
    return parsed


# ---- transforms (pure; plans/items-design.md §5) ------------------------------

def apply_md(state: ItemsState, parsed: Parsed, spec: ItemsSpec) -> tuple[ItemsState, list[str]]:
    """Merge a parsed md section into the state (the sync path)."""
    new = copy.deepcopy(state)
    notes = list(parsed.notes)
    if parsed.problems:
        raise ItemsError(parsed.problems[0])
    # groups: md order decides; a missing heading deletes the group; a heading
    # without a letter gets the next allocated one
    old_letters = {g.letter for g in new.groups}
    new_groups: list[Group] = []
    seen: set[str] = set()
    for pg in parsed.groups:
        if pg.letter:
            if pg.letter not in old_letters:
                raise ItemsError(
                    f"unknown group {pg.letter!r} — never allocated for this "
                    f"record (drop the letter to create a new group)")
            if pg.letter in seen:
                raise ItemsError(f"duplicate group heading {pg.letter!r}")
            seen.add(pg.letter)
            letter = pg.letter
        else:
            if new.last_group >= len(GROUP_LETTERS):
                raise ItemsError(
                    f"the record has {len(GROUP_LETTERS)} groups (the A-Z cap) — "
                    f"cannot add another")
            letter = GROUP_LETTERS[new.last_group]
            new.last_group += 1
        new_groups.append(Group(letter, pg.title, pg.trailer))
    new.groups = new_groups
    new.intro = parsed.intro
    # items, in md order (ungrouped first, then the groups in md order)
    old_by = {i.no: i for i in state.items}
    new_items: list[Item] = []
    seen_nos: set[ItemNo] = set()
    pending = [(UNGROUPED, p) for p in parsed.ungrouped]
    for pg, g in zip(parsed.groups, new_groups, strict=True):
        pending += [(g.letter, p) for p in pg.items]
    for grp, p in pending:
        if p.no is None:
            seq = new.last_seq.get(grp, 0) + 1
            _capacity(grp, seq)
            new.last_seq[grp] = seq
            new_items.append(Item(
                no=ItemNo(grp, seq), text=p.text, state=p.state, note=p.note,
                owner=p.owner, due=p.due, created=dbmod.utc_now(),
                closed_at=dbmod.utc_now() if p.state in spec.closed_states else "",
                trailer=p.trailer))
            continue
        if p.no in seen_nos:
            raise ItemsError(f"duplicate item #{p.no} — each number may appear once")
        seen_nos.add(p.no)
        old = old_by.pop(p.no, None)
        if old is None:
            raise ItemsError(f"item #{p.no} does not exist for this record — "
                             f"drop the number to add it as new")
        closed_now = p.state in spec.closed_states
        closed_before = old.state in spec.closed_states
        if old.no.grp == grp:
            no = old.no
        else:  # cross-group move: fresh number, old one void
            seq = new.last_seq.get(grp, 0) + 1
            _capacity(grp, seq)
            new.last_seq[grp] = seq
            no = ItemNo(grp, seq)
            notes.append(f"#{old.no} → #{no}")
        new_items.append(Item(
            no=no, text=p.text, state=p.state, note=p.note, owner=p.owner,
            due=p.due, created=old.created, commit=old.commit,
            closed_at=old.closed_at if (closed_now and closed_before)
            else (dbmod.utc_now() if closed_now else ""),
            trailer=p.trailer))
    new.items = new_items
    new.check()
    return new, notes


def add(state: ItemsState, spec: ItemsSpec, text: str, group: str | None = None,
        after: ItemNo | None = None, state_name: str | None = None,
        note: str = "", owner: str = "", due: str = "") -> tuple[ItemsState, list[str]]:
    new = copy.deepcopy(state)
    _validate_item(spec, text, owner, due)
    grp = (group or "").upper()
    if grp and grp not in {g.letter for g in new.groups}:
        raise ItemsError(f"group {grp} does not exist (create it: proj item group add)")
    st = _state_name(spec, state_name)
    pos = None
    if after is not None:
        ref = _by_no(new, after)
        if ref.no.grp != grp:
            raise ItemsError(
                f"item #{ref.no} is in group {ref.no.grp or '(none)'} — "
                f"'after' must name an item of the target group")
        pos = new.items.index(ref) + 1
    seq = new.last_seq.get(grp, 0) + 1
    _capacity(grp, seq)
    new.last_seq[grp] = seq
    item = Item(no=ItemNo(grp, seq), text=text.strip(), state=st, note=note,
                owner=owner, due=due, created=dbmod.utc_now(),
                closed_at=dbmod.utc_now() if st in spec.closed_states else "")
    if pos is None:
        pos = _grp_bounds(new, grp)[1]
    new.items.insert(pos, item)
    new.check()
    return new, []


def set_state(state: ItemsState, spec: ItemsSpec, no: ItemNo, state_name: str,
              note: str | None = None, commit: str = "") -> tuple[ItemsState, list[str]]:
    new = copy.deepcopy(state)
    st = _state_name(spec, state_name)
    item = _by_no(new, no)
    changed = item.state != st
    if not changed and note is None and not commit:
        return new, []  # nothing to do — no empty changelog entry (old bug)
    if changed:
        was_closed = item.state in spec.closed_states
        is_closed = st in spec.closed_states
        item.state = st
        if is_closed and not was_closed:
            item.closed_at = dbmod.utc_now()
        elif not is_closed:
            item.closed_at = ""
    if note is not None:
        item.note = note
    if commit:
        item.commit = commit
    return new, []


def edit(state: ItemsState, spec: ItemsSpec, no: ItemNo,
         text: str | None = None, note: str | None = None,
         owner: str | None = None, due: str | None = None) -> tuple[ItemsState, list[str]]:
    new = copy.deepcopy(state)
    item = _by_no(new, no)
    if text is not None:
        if text == "":
            item.text = ""
        else:
            _validate_item(spec, text)
            item.text = text
    if note is not None:
        item.note = note
    if owner is not None:
        if owner and "owner" not in spec.fields:
            raise ItemsError(f"{spec.noun}s of this record type have no owner field")
        item.owner = owner
    if due is not None:
        if due:
            if "due" not in spec.fields:
                raise ItemsError(f"{spec.noun}s of this record type have no due field")
            _validate_item(spec, item.text, due=due)
        item.due = due
    return new, []


def move(state: ItemsState, spec: ItemsSpec, no: ItemNo,
         group: str | None = None, ungroup: bool = False,
         after: ItemNo | None = None, first: bool = False) -> tuple[ItemsState, list[str]]:
    """Exactly one of group / ungroup / after / first. Cross-group → fresh
    number (the old one is void); within a group → order only."""
    if (group is not None) + ungroup + (after is not None) + first != 1:
        raise ItemsError("pass exactly one of group / ungroup / after / first")
    new = copy.deepcopy(state)
    notes: list[str] = []
    item = _by_no(new, no)
    grp = (group or "").upper() if group is not None else (UNGROUPED if ungroup else item.no.grp)
    if group is not None and grp not in {g.letter for g in new.groups}:
        raise ItemsError(f"group {grp} does not exist")
    if after is not None:
        ref = _by_no(new, after)
        if ref.no.grp != grp:
            raise ItemsError(
                f"item #{ref.no} is in group {ref.no.grp or '(none)'} — "
                f"'after' must name an item of the target group")
    new.items.pop(new.items.index(item))
    if grp == item.no.grp:
        pos = (new.items.index(_by_no(new, after)) + 1) if after is not None \
            else _grp_bounds(new, grp)[0 if first else 1]
        new.items.insert(pos, item)
    else:
        seq = new.last_seq.get(grp, 0) + 1
        _capacity(grp, seq)
        new.last_seq[grp] = seq
        item = Item(no=ItemNo(grp, seq), text=item.text, state=item.state,
                    note=item.note, owner=item.owner, due=item.due,
                    created=item.created, closed_at=item.closed_at,
                    commit=item.commit, trailer=item.trailer)
        notes.append(f"#{no} → #{item.no}")
        pos = (new.items.index(_by_no(new, after)) + 1) if after is not None \
            else _grp_bounds(new, grp)[0 if first else 1]
        new.items.insert(pos, item)
    new.check()
    return new, notes


def remove(state: ItemsState, spec: ItemsSpec, no: ItemNo) -> tuple[ItemsState, list[str]]:
    new = copy.deepcopy(state)
    item = _by_no(new, no)
    new.items.remove(item)
    new.check()
    return new, []


def group_add(state: ItemsState, spec: ItemsSpec, title: str) -> tuple[ItemsState, list[str]]:
    new = copy.deepcopy(state)
    if not title.strip():
        raise ItemsError("group title must be non-empty")
    if new.last_group >= len(GROUP_LETTERS):
        raise ItemsError(f"the record has {len(GROUP_LETTERS)} groups (the A-Z cap)")
    letter = GROUP_LETTERS[new.last_group]
    new.last_group += 1
    new.groups.append(Group(letter, title.strip()))
    new.check()
    return new, []


def group_rename(state: ItemsState, spec: ItemsSpec, letter: str,
                 title: str) -> tuple[ItemsState, list[str]]:
    new = copy.deepcopy(state)
    if not title.strip():
        raise ItemsError("group title must be non-empty")
    g = _by_letter(new, letter.upper())
    g.title = title.strip()
    return new, []


def group_mv(state: ItemsState, spec: ItemsSpec, letter: str,
             after: str | None = None, first: bool = False) -> tuple[ItemsState, list[str]]:
    if (after is not None) + first != 1:
        raise ItemsError("pass exactly one of after / first")
    new = copy.deepcopy(state)
    letter = letter.upper()
    g = _by_letter(new, letter)
    new.groups.remove(g)
    if after is not None:
        ref = _by_letter(new, after.upper())
        new.groups.insert(new.groups.index(ref) + 1, g)
    else:
        new.groups.insert(0, g)
    return new, []


def group_rm(state: ItemsState, spec: ItemsSpec, letter: str,
             items: str | None = None) -> tuple[ItemsState, list[str]]:
    """Delete a group; when it still holds items, `items` must be 'ungroup'
    (fresh ungrouped numbers) or 'delete'."""
    new = copy.deepcopy(state)
    letter = letter.upper()
    _by_letter(new, letter)
    members = [i for i in new.items if i.no.grp == letter]
    if members:
        if items not in ("ungroup", "delete"):
            raise ItemsError(
                f"group {letter} holds {len(members)} item(s) — "
                f"pass items='ungroup' or items='delete'")
        if items == "delete":
            new.items = [i for i in new.items if i.no.grp != letter]
        else:
            end = next((i for i, x in enumerate(new.items) if x.no.grp),
                       len(new.items))
            new.items = [i for i in new.items if i.no.grp != letter]
            kept: list[Item] = []
            for i in members:
                seq = new.last_seq.get(UNGROUPED, 0) + 1
                _capacity(UNGROUPED, seq)
                new.last_seq[UNGROUPED] = seq
                kept.append(Item(no=ItemNo(UNGROUPED, seq), text=i.text, state=i.state,
                                 note=i.note, owner=i.owner, due=i.due,
                                 created=i.created, closed_at=i.closed_at,
                                 commit=i.commit, trailer=i.trailer))
            new.items[end:end] = kept
    new.groups = [g for g in new.groups if g.letter != letter]
    new.check()
    return new, []


# ---- validate + changelog (plans/items-design.md §5.3-5.4) -------------------

def validate(old: ItemsState, new: ItemsState, spec: ItemsSpec,
             record_status: str, gates_items: list[str] | None = None) -> None:
    ungrouped = [i for i in new.items if not i.no.grp]
    if len(ungrouped) > UNGROUPED_CAP:
        raise ItemsError(f"the ungrouped list holds more than {UNGROUPED_CAP} items — "
                         f"split the record")
    for g in new.groups:
        n = sum(1 for i in new.items if i.no.grp == g.letter)
        if n > GROUP_CAP:
            raise ItemsError(f"group {g.letter} holds more than {GROUP_CAP} items — "
                             f"split the record")
    if len(new.groups) > len(GROUP_LETTERS):
        raise ItemsError(f"the record holds more than {len(GROUP_LETTERS)} groups — "
                         f"split the record")
    if record_status in (gates_items or []):
        un = [i for i in new.items if i.state not in spec.closed_states]
        if un:
            raise ItemsError(
                f"record is {record_status!r} (a closed state) — "
                f"#{un[0].no} {un[0].text} is unclosed; un-close the record "
                f"first, then edit items")
    new.check()


def diff_log(old: ItemsState, new: ItemsState,
             notes: list[str]) -> list[tuple[str, str]]:
    """(category, message) entries for the changelog — the single source for
    both sync and commands. Order-only changes log nothing."""
    out: list[tuple[str, str]] = []
    old_by = {i.no: i for i in old.items}
    new_by = {i.no: i for i in new.items}
    moves = [(m.group(1), m.group(2)) for m in (MOVE_NOTE_RE.match(n) for n in notes) if m]
    moved_old = {a for a, _ in moves}
    moved_new = {b for _, b in moves}
    for a, b in moves:
        out.append(("Changed", f"item {a} → {b}"))
    for no, i in old_by.items():
        if no in new_by or no in moved_old:
            continue
        out.append(("Removed", f"item #{no}: {i.text}"))
    for no, i in new_by.items():
        if no in old_by or no in moved_new:
            continue
        out.append(("Added", f"item #{no}: {i.text}"))
    for no, i in new_by.items():
        o = old_by.get(no)
        if o is None:
            continue
        if i.state != o.state:
            out.append(("Changed", f"item #{no} {o.state} → {i.state}"))
        elif i.text != o.text or i.note != o.note or i.owner != o.owner or i.due != o.due:
            out.append(("Changed", f"item #{no} edited"))
    old_g = {g.letter: g for g in old.groups}
    new_g = {g.letter: g for g in new.groups}
    for L, g in new_g.items():
        if L not in old_g:
            out.append(("Added", f"group {L} · {g.title}"))
        elif old_g[L].title != g.title:
            out.append(("Changed", f"group {L} renamed"))
    for L in old_g:
        if L not in new_g:
            out.append(("Removed", f"group {L}"))
    return out


# ---- DB access + single write path (plans/items-design.md §6.2) ---------------

def _stored_intro(row: dict, spec: ItemsSpec) -> str:
    # deviation from the design (see the report): the intro lives in the
    # records.sections JSON (the old mechanism), not in a new table
    return json.loads(row["sections"] or "{}").get(spec.items.section.lower(), "")


def load(db, record_id: str, intro: str = "") -> ItemsState:
    data = db.items_load(record_id)
    state = ItemsState(intro=intro, last_seq=dict(data["last_seq"]),
                       last_group=data["last_group"])
    for g in data["groups"]:
        state.groups.append(Group(g["letter"], g["title"], g.get("trailer", "")))
    rows = data["items"]
    ordered = [r for r in rows if not r["grp"]]
    for g in state.groups:
        ordered += [r for r in rows if r["grp"] == g.letter]
    for r in ordered:
        state.items.append(Item(no=ItemNo(r["grp"], r["seq"]), text=r["text"],
                                state=r["state"], note=r["note"], owner=r["owner"],
                                due=r["due"], created=r["created"],
                                closed_at=r["closed_at"], commit=r["commit"],
                                trailer=r.get("trailer", "")))
    return state


def progress(state: ItemsState, spec) -> tuple[int, int]:
    """(closed, total) — the n/m of a task row (all groups counted).
    `spec`: the ItemsSpec or its RecordTypeSpec."""
    spec = _it_spec(spec)
    closed = sum(1 for i in state.items if i.state in spec.closed_states)
    return closed, len(state.items)


def open_items(state: ItemsState, spec) -> list[str]:
    """The unclosed items as '#no text' — the gates.items message.
    `spec`: the ItemsSpec or its RecordTypeSpec."""
    spec = _it_spec(spec)
    return [f"#{i.no} {i.text}" for i in state.items
            if i.state not in spec.closed_states]


def commit(db, spec: RecordTypeSpec, row: dict, old: ItemsState, new: ItemsState,
           notes: list[str]) -> list[str]:
    """The single write path: validate → invariant → changelog → save → promote."""
    it = spec.items
    assert it is not None
    validate(old, new, it, row["status"], spec.gates_items)
    for category, message in diff_log(old, new, notes):
        db.add_log(row["id"], message, category)
    if _stored_intro(row, spec) != new.intro:
        sections = json.loads(row["sections"] or "{}")
        sections[it.section.lower()] = new.intro
        db.set(spec, row["id"], sections=json.dumps(sections, ensure_ascii=False))
    db.items_save(
        row["id"],
        [{"grp": i.no.grp, "seq": i.no.seq, "text": i.text, "state": i.state,
          "note": i.note, "owner": i.owner, "due": i.due, "created": i.created,
          "closed_at": i.closed_at, "commit": i.commit, "trailer": i.trailer}
         for i in new.items],
        [{"letter": g.letter, "title": g.title, "trailer": g.trailer}
         for g in new.groups],
        new.last_seq, new.last_group)
    if (it.promote_from and it.promote_to and row["status"] in it.promote_from
            and any(i.state in it.closed_states for i in new.items)):
        db.set(spec, row["id"], status=it.promote_to)
        db.add_log(row["id"], f"auto-promoted → {it.promote_to} (first closed item)",
                   "Changed")
        notes.append(f"{row['id']} promoted → {it.promote_to} (first closed item)")
    return notes


def sync_section(db, spec: RecordTypeSpec, row: dict, md_text: str,
                 source: str = "", strip_numbers: bool = False) -> tuple[list[str], list[str]]:
    """Store's sync entry: parse → apply_md → commit. On problems nothing is
    written and the md stays as-is. The stored section is re-rendered from the
    DB afterwards — the intro/trailers travel with the state.
    `strip_numbers` (adopt channel): every line is treated as a NEW item — the
    stored record is fresh, so no number can refer to it."""
    it = spec.items
    assert it is not None
    parsed = parse_section(md_text, it)
    if parsed.problems:
        return parsed.problems, []
    if strip_numbers:
        for p in list(parsed.ungrouped) + [x for g in parsed.groups for x in g.items]:
            p.no = None
    old = load(db, row["id"], intro=_stored_intro(row, spec))
    try:
        new, notes = apply_md(old, parsed, it)
        notes = commit(db, spec, row, old, new, notes)
    except ItemsError as e:
        return [str(e)], []
    old_by = {i.no: i for i in old.items}
    new_by = {i.no: i for i in new.items}
    sync_notes: list[str] = []
    for i in new.items:
        if i.no not in old_by:
            sync_notes.append(f"new item #{i.no} ({i.text})")
    for i in old.items:
        if i.no not in new_by:
            sync_notes.append(f"item #{i.no} deleted ({i.text})")
    sync_notes += notes  # moves, emoji defaults, promote
    return [], [f"{source}: {n}" for n in sync_notes]


def drift(db, spec: RecordTypeSpec, row: dict, md_text: str) -> bool:
    """True when the md's section differs from the DB render (normalized)."""
    state = load(db, row["id"], intro=_stored_intro(row, spec))
    return normalize(md_text) != render_section(state, spec)
