"""Item checklist operations — the entry points the CLI and MCP call (they take
a ProjConfig, return a rendered string or a list of lines, and raise
ItemsError). Depends on store (which engine/items.py deliberately does NOT —
see plans/items-design.md §7 for the module split). Every mutating op runs the
same flow: resolve the ref → check the md is in sync → pure transform → commit
(one changelog) → re-render → return the fresh item list."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from proj.core.config import ProjConfig
from proj.engine import items
from proj.engine import store as rec


def _record_section_md(cfg: ProjConfig, spec, row: dict) -> str:
    """The record's items section as written in its md ('' when the file is absent)."""
    p = rec.file_path(cfg, spec, row)
    if not p.is_file():
        return ""
    _mk, _u, raw = rec.parse_record(p.read_text(encoding="utf-8"))
    return raw.get(spec.items.section.lower(), "") or ""


def _load_state(db, spec, row: dict) -> items.ItemsState:
    state = items.load(db, row["id"], spec)
    state.intro = items._intro_of(spec, row)
    return state


def _parse_ref(cfg: ProjConfig, ref: str, need_item: bool) -> tuple[object, object, str | None]:
    """(spec, row, item number) for a ref; the number (norm_no'd) when need_item."""
    rec_part, sep, no_part = ref.partition("#")
    no = None
    if need_item:
        if not sep:
            raise items.ItemsError(
                f"pass an item ref like {ref!r} plus #nn (e.g. {ref}#001)")
        try:
            no = items.norm_no(no_part)
        except items.ItemsError as e:
            raise items.ItemsError(f"item part of {ref!r}: {e}") from None
    db = rec.open_db(cfg)
    try:
        spec, row = rec.ref_record(cfg, db, rec_part)
    finally:
        db.close()
    if row is None:
        raise items.ItemsError(f"{rec_part!r} does not match any record")
    if spec.items is None:
        raise items.ItemsError(f"{spec.record_type} has no items")
    return spec, row, no


@dataclass
class _Ctx:
    cfg: ProjConfig
    spec: object
    db: object
    row: dict
    it: object        # the ItemsSpec
    state: items.ItemsState
    no: str | None

    def save(self, new: items.ItemsState, notes: list[str]) -> str:
        """Commit (one changelog), re-render the record and views, and return
        the fresh item list."""
        items.commit(self.db, self.spec, self.row, self.state, new, notes)
        rec.render_all(self.cfg)
        row = self.db.get(self.spec, self.row["id"])
        state = _load_state(self.db, self.spec, row)
        return items.render_section(state, self.it)


@contextmanager
def _editing(cfg: ProjConfig, ref: str, need_item: bool):
    """The whole op flow (plans/items-design.md §7): resolve ref → refuse on
    unsynced md → yield _Ctx → (caller computes new state) → ctx.save re-renders."""
    spec, row, no = _parse_ref(cfg, ref, need_item)
    rec.ensure_synced(cfg)  # A1: unsynced edits are ingested, never refused
    db = rec.open_db(cfg)
    try:
        yield _Ctx(cfg, spec, db, row, spec.items, _load_state(db, spec, row), no)
    finally:
        db.close()


def _parse_after(ctx: _Ctx, s: str) -> str:
    """The --after value: '#003' / 'A01' (same record) or 'T-000001#003' —
    returned as the normalized number of THIS record."""
    rec_part, sep, no_part = s.partition("#")
    if sep:
        if rec_part and rec_part.upper() != ctx.row["id"].upper():
            raise items.ItemsError(f"--after {s!r} is not an item of {ctx.row['id']}")
        try:
            return items.norm_no(no_part)
        except items.ItemsError as e:
            raise items.ItemsError(f"--after {s!r}: {e}") from None
    try:
        return items.norm_no(s)
    except items.ItemsError as e:
        raise items.ItemsError(f"--after {s!r}: {e}") from None


# ---- ops (one per CLI/MCP command) ---------------------------------------------

def add(cfg: ProjConfig, ref: str, text: str, group: str | None = None,
        after: str | None = None, state: str | None = None,
        note: str = "", owner: str = "", due: str = "") -> str:
    with _editing(cfg, ref, need_item=False) as c:
        after_no = _parse_after(c, after) if after is not None else None
        new, notes = items.add(c.state, c.it, text, group=group, after=after_no,
                               state_name=state or "", note=note,
                               owner=owner, due=due)
        return c.save(new, notes)


def set_state(cfg: ProjConfig, ref: str, state_name: str, note: str | None = None,
              commit: str = "") -> str:
    with _editing(cfg, ref, need_item=True) as c:
        new, notes = items.set_state(c.state, c.it, c.no, state_name, note, commit)
        return c.save(new, notes)


def check(cfg: ProjConfig, ref: str, note: str | None = None, commit: str = "") -> str:
    with _editing(cfg, ref, need_item=True) as c:
        first = next(s.name for s in c.it.states if s.closed)
        new, notes = items.set_state(c.state, c.it, c.no, first, note, commit)
        return c.save(new, notes)


def uncheck(cfg: ProjConfig, ref: str) -> str:
    with _editing(cfg, ref, need_item=True) as c:
        first = c.it.states[0].name
        new, notes = items.set_state(c.state, c.it, c.no, first)
        return c.save(new, notes)


def edit(cfg: ProjConfig, ref: str, text: str | None = None, note: str | None = None,
         owner: str | None = None, due: str | None = None) -> str:
    with _editing(cfg, ref, need_item=True) as c:
        new, notes = items.edit(c.state, c.it, c.no, text=text, note=note,
                                owner=owner, due=due)
        return c.save(new, notes)


def move(cfg: ProjConfig, ref: str, group: str | None = None, ungroup: bool = False,
         after: str | None = None, first: bool = False) -> str:
    with _editing(cfg, ref, need_item=True) as c:
        after_no = _parse_after(c, after) if after is not None else None
        new, notes = items.move(c.state, c.it, c.no, group=group, ungroup=ungroup,
                                after=after_no, first=first)
        return c.save(new, notes)


def remove(cfg: ProjConfig, ref: str) -> str:
    with _editing(cfg, ref, need_item=True) as c:
        new, notes = items.remove(c.state, c.it, c.no)
        return c.save(new, notes)


def group_add(cfg: ProjConfig, ref: str, title: str) -> str:
    with _editing(cfg, ref, need_item=False) as c:
        new, notes = items.group_add(c.state, c.it, title)
        return c.save(new, notes)


def group_rename(cfg: ProjConfig, ref: str, letter: str, title: str) -> str:
    with _editing(cfg, ref, need_item=False) as c:
        new, notes = items.group_rename(c.state, c.it, letter, title)
        return c.save(new, notes)


def group_mv(cfg: ProjConfig, ref: str, letter: str, after: str | None = None,
             first: bool = False) -> str:
    with _editing(cfg, ref, need_item=False) as c:
        after_no = _parse_after(c, after) if after is not None else None
        new, notes = items.group_mv(c.state, c.it, letter, after=after_no,
                                    first=first)
        return c.save(new, notes)


def group_rm(cfg: ProjConfig, ref: str, letter: str, items_mode: str | None = None) -> str:
    """`items_mode`: 'ungroup' (fresh ungrouped numbers) or 'delete' — required
    when the group still holds items."""
    with _editing(cfg, ref, need_item=False) as c:
        new, notes = items.group_rm(c.state, c.it, letter, items=items_mode)
        return c.save(new, notes)


def ls(cfg: ProjConfig, ref: str | None = None, group: str | None = None,
       open_only: bool = False, record_type: str | None = None,
       owner: str | None = None, overdue: bool = False) -> list[str]:
    """Item lines as `ID#A01  state  text  [owner]  [due]` (one record or all).
    A ref that exists but has no items (or no such record) is an error — the
    old silent empty result is gone."""
    reg = rec.load_registry(cfg.root)
    if record_type is not None and record_type not in reg:
        raise items.ItemsError(
            f"unknown record_type {record_type!r} (expected: "
            f"{', '.join(sorted(reg))})")
    db = rec.open_db(cfg)
    try:
        out: list[str] = []
        today = date.today().isoformat()
        grp = group.upper() if group else None

        def scan(spec, row):
            if spec.items is None:
                return
            if record_type is not None and spec.record_type != record_type:
                return
            for i in _load_state(db, spec, row).live():
                if i.is_group:
                    continue
                if grp and items.group_of(i.no) != grp:
                    continue
                if open_only and i.state in spec.items.closed_states:
                    continue
                if owner and i.owner != owner:
                    continue
                if overdue and (not i.due or i.due >= today
                               or i.state in spec.items.closed_states):
                    continue
                parts = [f"{row['id']}#{i.no}", i.state, i.text]
                if i.owner:
                    parts.append(i.owner)
                if i.due:
                    parts.append(i.due)
                out.append("  ".join(parts))

        if ref is not None:
            spec, row = rec.ref_record(cfg, db, ref.partition("#")[0])
            if row is None:
                raise items.ItemsError(f"{ref!r} does not match any record")
            if spec.items is None:
                raise items.ItemsError(f"{spec.record_type} has no items")
            scan(spec, row)
        else:
            for spec in reg.values():
                for row in db.all(spec):
                    scan(spec, row)
        return out
    finally:
        db.close()
