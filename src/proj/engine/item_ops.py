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
    return raw.get(spec.primary_items.section.lower(), "") or ""


def _load_state(db, row: dict, it) -> items.ItemsState:
    state = items.load(db, row["id"], it)
    state.intro = items._intro_of(it, row)
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
    if spec.primary_items is None:
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
        the notes (`note: …` lines — renumbering moves, promote, …) followed by
        the fresh item list."""
        notes = items.commit(self.db, self.spec, self.row, self.state, new, notes, it=self.it)
        # R3: render on the SAME connection — it reads the (still uncommitted)
        # item write; a fresh connection would not see it under WAL
        rec.render_all(self.cfg, db=self.db)
        row = self.db.get(self.spec, self.row["id"])
        state = _load_state(self.db, row, self.it)
        listing = items.render_section(state, self.it)
        return "\n".join([*(f"note: {n}" for n in notes), listing]).strip("\n")


@contextmanager
def _editing(cfg: ProjConfig, ref: str, need_item: bool,
             section: str | None = None, task: str = ""):
    """The whole op flow (plans/items-design.md §7): resolve ref → refuse on
    unsynced md → yield _Ctx → (caller computes new state) → ctx.save re-renders.
    S.2: the whole op (incl. the save + re-render) holds the project write lock."""
    with rec.write_lock(cfg, "item"):
        spec, row, no = _parse_ref(cfg, ref, need_item)
        rec.ensure_synced(cfg)  # A1: unsynced edits are ingested, never refused
        db = rec.open_db(cfg)
        it = spec.primary_items
        if need_item and no:
            # the item is addressed by number: find the checklist that carries
            # it (P1: a record may have several; the number is record-unique)
            for x in spec.items:
                st = items.load(db, row["id"], x)
                if st.by_no(no) is not None:
                    it = x
                    break
        elif section:  # a specific checklist (P1: a record may have several)
            if it is None or all(x.section != section for x in spec.items):
                raise items.ItemsError(
                    f"no checklist named {section!r} on {row['id']} "
                    f"(has: {', '.join(x.section for x in spec.items)})")
            it = next(x for x in spec.items if x.section == section)
        elif task and spec.wip_items is not None:
            # a new item that references a task belongs to the record's WIP
            # checklist (the one that shows in WIP.md / gates on archive)
            it = spec.wip_items
        try:
            yield _Ctx(cfg, spec, db, row, it, _load_state(db, row, it), no)
        finally:
            db.close()


def _check_task_ref(ctx: _Ctx, task: str) -> None:
    """K3/P5: a `→ task` reference must name an EXISTING record that is a task —
    anything else is an error (use `related` for other types, or write the id
    in the item text, which links without a state tie)."""
    if not task:
        return
    canonical = rec.canonical_id(ctx.cfg, task)
    row = ctx.db.conn.execute(
        "SELECT type FROM records WHERE id=?", (canonical,)).fetchone()
    if row is None:
        raise items.ItemsError(f"task ref {task!r} does not name an existing record")
    if row[0] != "task":
        raise items.ItemsError(
            f"task ref {task!r} is a {row[0]} — only tasks may be referenced "
            f"(use related, or write the id in the item text)")


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
        note: str = "", owner: str = "", due: str = "",
        task: str = "", section: str | None = None) -> str:
    with _editing(cfg, ref, need_item=False, section=section, task=task) as c:
        after_no = _parse_after(c, after) if after is not None else None
        _check_task_ref(c, task)  # K3/P5: the ref must be an existing task
        new, notes = items.add(c.state, c.it, text, group=group, after=after_no,
                               state_name=state or "", note=note,
                               owner=owner, due=due, task=task)
        return c.save(new, notes)


def set_state(cfg: ProjConfig, ref: str, state_name: str, note: str | None = None,
              commit: str = "", section: str | None = None) -> str:
    with _editing(cfg, ref, need_item=True, section=section) as c:
        new, notes = items.set_state(c.state, c.it, c.no, state_name, note, commit)
        return c.save(new, notes)


def check(cfg: ProjConfig, ref: str, note: str | None = None, commit: str = "",
          section: str | None = None) -> str:
    with _editing(cfg, ref, need_item=True, section=section) as c:
        new, notes = items.set_state(c.state, c.it, c.no, items.DONE, note, commit)
        return c.save(new, notes)


def uncheck(cfg: ProjConfig, ref: str, section: str | None = None) -> str:
    with _editing(cfg, ref, need_item=True, section=section) as c:
        new, notes = items.set_state(c.state, c.it, c.no, items.OPEN)
        return c.save(new, notes)


def edit(cfg: ProjConfig, ref: str, text: str | None = None, note: str | None = None,
         owner: str | None = None, due: str | None = None, task: str | None = None,
         reassign: bool = False, section: str | None = None) -> str:
    with _editing(cfg, ref, need_item=True, section=section) as c:
        if task:
            _check_task_ref(c, task)  # K3/P5: the new ref must be an existing task
        # S.3: handing an item to a DIFFERENT owner (or clearing the owner) is
        # refused by default — the current owner keeps it unless the taker
        # passes --reassign. First claim (no owner yet) and re-asserting the
        # same owner are plain edits.
        if owner is not None:
            old = c.state.by_no(c.no)
            if old is not None and old.owner and old.owner != owner and not reassign:
                raise items.ItemsError(
                    f"#{c.no} is owned by {old.owner} — pass --reassign to hand it over")
        new, notes = items.edit(c.state, c.it, c.no, text=text, note=note,
                                owner=owner, due=due, task=task)
        out = c.save(new, notes)
        if owner is not None and reassign:
            nxt = new.by_no(c.no)
            if (old is not None and nxt is not None
                    and old.owner and old.owner != nxt.owner):
                # the generic `item #no edited` line (diff_log) does not name
                # the hand-over — S.3 wants the owner change logged explicitly
                c.db.add_log(c.row["id"],
                             f"item #{c.no} owner {old.owner} → {nxt.owner or '(cleared)'}",
                             category="Changed")
        return out


def sync_owner_notes(db, spec, row: dict, section_md: str) -> list[str]:
    """S.3: the md's items section names a DIFFERENT owner than the DB — a
    human hand-over (edit the file by hand / WIP.md). Sync accepts it (a
    direct edit is deliberate), but the note names it: `item #A01 owner
    alex → billy` (the changelog gets the generic `edited` line)."""
    stored = {i.no: i.owner for i in items.load(db, row["id"], spec).live()}
    parsed = items.parse_section(section_md, spec)
    return [f"item #{it.no} owner {stored[it.no]} → {it.owner or '(cleared)'}"
            for it in items._all_items(parsed)
            if it.no in stored and stored[it.no] and stored[it.no] != it.owner]


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
    """Item lines as `ID#A01  state  text  [owner]  [due]` (one record or all)."""
    out = []
    for r in ls_rows(cfg, ref, group=group, open_only=open_only,
                     record_type=record_type, owner=owner, overdue=overdue):
        parts = [r["ref"], r["state"], r["text"]]
        if r["owner"]:
            parts.append(r["owner"])
        if r["due"]:
            parts.append(r["due"])
        out.append("  ".join(parts))
    return out


def ls_rows(cfg: ProjConfig, ref: str | None = None, group: str | None = None,
            open_only: bool = False, record_type: str | None = None,
            owner: str | None = None, overdue: bool = False) -> list[dict]:
    """The items `ls` lists, as dicts (ref, record, no, group, state, closed,
    text, note, owner, due) — the --json / as_json form. A ref that exists
    but has no items (or no such record) is an error — the old silent empty
    result is gone."""
    reg = rec.load_registry(cfg.root)
    if record_type is not None and record_type not in reg:
        raise items.ItemsError(
            f"unknown record_type {record_type!r} (expected: "
            f"{', '.join(sorted(reg))})")
    db = rec.open_db(cfg)
    try:
        out: list[dict] = []
        today = date.today().isoformat()
        grp = group.upper() if group else None

        def scan(spec, row):
            if not spec.items:
                return
            if record_type is not None and spec.record_type != record_type:
                return
            for itspec in spec.items:
                closed = set(itspec.closed_states)
                for i in items.load(db, row["id"], itspec).live():
                    if i.is_group:
                        continue
                    if grp and items.group_of(i.no) != grp:
                        continue
                    if open_only and i.state in closed:
                        continue
                    if owner and i.owner != owner:
                        continue
                    if overdue and (not i.due or i.due >= today
                                   or i.state in closed):
                        continue
                    out.append({"ref": f"{row['id']}#{i.no}", "record": row["id"],
                                "no": i.no, "group": items.group_of(i.no),
                                "state": i.state,
                                "closed": i.state in closed,
                                "text": i.text, "note": i.note, "owner": i.owner,
                                "due": i.due})

        if ref is not None:
            spec, row = rec.ref_record(cfg, db, ref.partition("#")[0])
            if row is None:
                raise items.ItemsError(f"{ref!r} does not match any record")
            if spec.primary_items is None:
                raise items.ItemsError(f"{spec.record_type} has no items")
            scan(spec, row)
        else:
            for spec in reg.values():
                for row in db.all(spec):
                    scan(spec, row)
        return out
    finally:
        db.close()
