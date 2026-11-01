"""MCP server for proj (stdio) — one tool per CLI command (D1), thin wrappers
over the same engine functions as the CLI.

Run: `proj-mcp` (registered in pyproject [project.scripts]).
Every tool takes an optional `root` (absolute project path, = the CLI `--root`);
default = the current directory (no walking up). Returns human-readable text;
on validation failure a clean 'error: …' message is returned (D4: ProjError and
sqlite3.Error alike, no tracebacks).

D2: tool names = `proj_` + the CLI command path (proj_item_add, proj_task_move,
…); parameters are `id`, not `id_`; `record_type` is OPTIONAL wherever the id
carries its type (the id decides; pass it only to be explicit). The former
`proj_update_record` is `proj_update` now (the global `update` is gone; the
CLI's per-record `update` is the only one left). `proj_verb` stays the generic
`proj_verb(id, verb, …)` — the one tool whose name does not follow a command
path (verbs differ per type).

D3: each tool is registered ONCE — FastMCP accepts sync functions, so there is
no async wrapper with a duplicated docstring. D5: `show`/`list` formatting
lives in engine/present.py, shared with the CLI.

IDs are `{code}-{digits}` (7 chars, e.g. DEC-0001, T-000001); the suffix is
DIGITS ONLY — no letter overflow (once the all-digit space is used up, new ids
raise). Any zero-shortened form resolves: "1", "0001", "T-1" and "DEC-0001"
all hit the same record.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server import MCPServer

from proj import scaffold
from proj.check import check as run_checks
from proj.core.config import load_config, load_project_env
from proj.core.db import ProjError
from proj.core.templates import resolve_record_type
from proj.engine import item_ops, present
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.record_types import changelog as cl
from proj.record_types import task as taskmod

mcp = MCPServer("proj")

#: env var with the absolute project path — used when a tool call omits `root`
#: (same pattern as ecs-gpu's ECSGPU_HOME). Priority: tool `root` > $PROJ_HOME > cwd.
PROJ_HOME_ENV = "PROJ_HOME"


def _cfg(root: str | None, strict: bool = True):
    root = root or os.environ.get(PROJ_HOME_ENV)
    try:
        cfg = load_config(Path(root) if root else None, strict=strict)
    except FileNotFoundError as e:
        raise ValueError(str(e)) from e
    load_project_env(cfg.root)  # D6: project .env = config defaults (OS env wins)
    return cfg


def _safe(fn, *args) -> str:
    """D4: run a tool body; ProjError (engine) and sqlite3.Error alike become a
    clean 'error: …' — never a raw traceback."""
    import sqlite3
    try:
        return fn(*args)
    except (ProjError, sqlite3.Error) as e:
        return f"error: {e}"
    except ValueError as e:
        return f"error: {e}"


def _rtype(record_type: str | None, reg: dict) -> str | None:
    """Canonical record_type (name or code, case-insensitive); None if unknown
    or not given."""
    if not record_type:
        return None
    try:
        return resolve_record_type(record_type, reg)
    except ValueError:
        return None


def _resolve(root: str | None, record_type: str | None, id_: str) -> tuple:
    """(cfg, spec, row) for a record id (D2): the id carries the type, so
    `record_type` is optional — an explicit one must still MATCH the id's type.
    Raises ValueError on any failure (clean 'error: …' via _safe)."""
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    if record_type:
        spec = _rtype(record_type, reg)
        if spec is None:
            raise ValueError("unknown record_type (name or code, case-insensitive)")
        db = rec.open_db(cfg)
        row = rec.by_id(cfg, reg[spec], db, id_)
        db.close()
        if row is None:
            raise ValueError(f"{spec} {id_} not found")
        return cfg, reg[spec], row
    db = rec.open_db(cfg)
    spec, row = rec.ref_record(cfg, db, id_)
    db.close()
    if row is None:
        raise ValueError(f"{id_} not found in any record_type")
    return cfg, spec, row


# ---- pure tool bodies (tests/test_mcp.py calls these directly; D3: the SAME
# function is registered as the MCP tool, so the docstring below IS the tool
# description — write it for the agent) ------------------------------------------

def proj_init(root: str | None = None, profile: str = "default") -> str:
    """Scaffold a new project: files, DB tables (lock wins over templates), seed,
    adopt. root: absolute project path (default: the current directory).
    profile: default plus additive names (cv, infra, quality, …); the choice is
    pinned in .proj.lock — repeat inits append new profiles."""
    cfg = _cfg(root, strict=False)
    try:
        created = scaffold.init(cfg, [profile])
    except ValueError as e:
        return f"error: {e}"
    if created:
        return "Created:\n" + "\n".join(f"  {c}" for c in created)
    return "Nothing to create — everything already exists."


def proj_new(record_type: str, title: str, root: str | None = None,
             supersede: str | None = None, parent: str | None = None,
             related: str | None = None, commit: str | None = None,
             summary: str | None = None) -> str:
    """Create a record from its template (DB insert + rendered md).
    record_type: registered type (decision/task/plan/debt/incident/experiment/…).
    supersede: ids being replaced, same type, comma-separated.
    related: related record ids, any type, comma-separated (unordered).
    parent: parent record id (task only). commit: git commit hash.
    summary: one-sentence summary."""
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return ("error: unknown record_type — name or code, case-insensitive "
                "(run proj_info for registered record_types)")
    flat_sup = [x.strip() for x in (supersede or "").split(",") if x.strip()]
    flat_rel = [x.strip() for x in (related or "").split(",") if x.strip()]
    # A3: same as the CLI — --parent only exists for task, error otherwise
    if parent and record_type != "task":
        return f"error: parent is only supported by task (got {record_type})"
    try:
        if record_type == "task":
            _, p = taskmod.new(cfg, title, parent, summary or "", commit or "",
                               flat_rel or None)
        else:
            obj = record_for(record_type, reg)
            _, p = obj.new(cfg, title, summary=summary or "", commit=commit or "",
                           supersede=flat_sup or None, related=flat_rel or None)
    except ValueError as e:
        return f"error: {e}"
    return f"Created {p.relative_to(cfg.root)}"


def proj_sync(files: str | None = None, root: str | None = None) -> str:
    """md → DB (validate, normalize) → re-render all md. files: comma-separated
    paths (default: all managed dirs). FAIL lines mean the DB was not fully
    updated."""
    cfg = _cfg(root)
    file_list = [f.strip() for f in (files or "").split(",") if f.strip()] or None
    try:
        problems, notes = rec.sync(cfg, file_list)
    except ValueError as e:
        return f"error: {e}"
    out = [f"FAIL  {p}" for p in problems] + [f"note: {n}" for n in notes]
    if problems:
        return "\n".join(out)
    return "OK — synced and re-rendered"


def proj_adopt(files: str | None = None, root: str | None = None) -> str:
    """Absorb pre-existing md files into the DB (id marker if present, else an
    id check). files: comma-separated paths (default: all managed dirs)."""
    cfg = _cfg(root)
    file_list = [Path(f.strip()) for f in (files or "").split(",") if f.strip()] or None
    problems, notes = rec.adopt(cfg, file_list)
    out = [f"FAIL  {p}" for p in problems] + [f"note: {n}" for n in notes]
    if problems:
        return "\n".join(out)
    return "OK — adopted and re-rendered"


def proj_render(ref: str | None = None, root: str | None = None) -> str:
    """Regenerate md from the DB (no md input). ref: comma-separated record ids
    and/or record_types (default: every record + every view). Unsynced hand
    edits are ingested first (problems abort before any file is written)."""
    cfg = _cfg(root)
    refs = [r.strip() for r in (ref or "").split(",") if r.strip()]
    try:
        rec.ensure_synced(cfg)
        if not refs:
            return f"Rendered {len(rec.render_all(cfg))} file(s)"
        reg = rec.load_registry(cfg.root)
        db = rec.open_db(cfg)
        out = []
        try:
            for r in refs:
                spec, row = rec.ref_record(cfg, db, r)
                if row is None:
                    if r not in reg:
                        raise ValueError(f"unknown record or record_type {r!r}")
                    spec, rows = reg[r], db.all(reg[r])
                else:
                    rows = [row]
                for row in rows:
                    p = rec.file_path(cfg, spec, row)
                    p.write_text(rec.render_record(spec, row, db, reg), encoding="utf-8")
                    out.append(f"Rendered {p.relative_to(cfg.root)}")
        finally:
            db.close()
        return "\n".join(out)
    except (ValueError, rec.ProjError) as e:
        return f"error: {e}"


def proj_check(root: str | None = None) -> str:
    """Validate DB ↔ lock ↔ templates ↔ md invariants. 'OK' or FAIL lines."""
    cfg = _cfg(root)
    problems = run_checks(cfg)
    if problems:
        return "\n".join(f"FAIL  {p}" for p in problems)
    return "OK"


def proj_ls(status: str | None = None, record_type: str | None = None,
            root: str | None = None) -> str:
    """List records (from DB), one line each (id, status, title). record_type:
    restrict to one type (name or code); omitted = every record_type.
    status: filter (display name, e.g. 'In Progress')."""
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    names = [n for n in ([record_type] if record_type else list(reg))
             if _rtype(n, reg) is not None]
    if record_type and not names:
        return "error: unknown record_type (name or code, case-insensitive)"
    rows = [r for n in names for r in record_for(n, reg).list(cfg, status)]
    return present.list_lines(rows, status)


def proj_list(record_type: str, status: str | None = None, root: str | None = None) -> str:
    """List records of one record_type (alias of proj_ls with the type given)."""
    return proj_ls(status=status, record_type=record_type, root=root)


def proj_show(id: str, record_type: str | None = None, history: bool = False,
              as_json: bool = False, root: str | None = None) -> str:
    """Show one record (from DB); the id carries its type so record_type is
    optional. history: append the full changelog. as_json: a JSON object
    (id/type/title/status/created/summary/parent/changelog) instead of text."""
    try:
        cfg, spec, row = _resolve(root, record_type, id)
    except ValueError as e:
        return f"error: {e}"
    db = rec.open_db(cfg)
    try:
        text, data = present.show(spec, row, db, history=history)
    finally:
        db.close()
    return json.dumps(data, indent=2, ensure_ascii=False) if as_json else text


def proj_open(id: str, root: str | None = None) -> str:
    """Open a record's md file path for the agent to read/edit (no editor).
    On the next write command (or proj_sync) the edit is ingested into the DB."""
    try:
        cfg, spec, row = _resolve(root, None, id)
    except ValueError as e:
        return f"error: {e}"
    return str(rec.file_path(cfg, spec, row).relative_to(cfg.root))


def proj_update(id: str, record_type: str | None = None, title: str | None = None,
                summary: str | None = None, message: str | None = None,
                root: str | None = None) -> str:
    """Update a record's title / summary (a title change renames its md file);
    message: optionally append a changelog entry ('Category: text'). The id
    carries the type — record_type is optional."""
    if title is None and summary is None and not message:
        return "error: nothing to update — pass title / summary / message"
    try:
        cfg, spec, row = _resolve(root, record_type, id)
    except ValueError as e:
        return f"error: {e}"
    try:
        row = rec.update_record(cfg, spec.record_type, row["id"],
                                title=title, summary=summary, message=message)
    except ValueError as e:
        return f"error: {e}"
    return f"{spec.record_type} {row['id']}: title → {row['title']}"


def proj_verb(id: str, verb: str, record_type: str | None = None,
              title: str | None = None, summary: str | None = None,
              message: str | None = None, root: str | None = None) -> str:
    """Run a template verb (a state-machine step) on a record, e.g.
    `('DEC-0002','accept')`, `('PLN-0001','activate')`, `('T-000001','done')`.
    The id carries the type — record_type is optional. verb must be one the
    type's template declares; `from: Any` verbs work from any status.
    title/summary/message: optionally also update them after the step
    (message appends a changelog entry, 'Category: text')."""
    try:
        cfg, spec, row = _resolve(root, record_type, id)
    except ValueError as e:
        return f"error: {e}"
    try:
        row = record_for(spec.record_type, rec.load_registry(cfg.root)).transition(
            cfg, verb, row["id"])
        if title is not None or summary is not None or message:
            row = rec.update_record(cfg, spec.record_type, row["id"],
                                    title=title, summary=summary, message=message)
    except ValueError as e:
        return f"error: {e}"
    return f"{spec.record_type} {row['id']}: status → {row['status']}"


def proj_info(root: str | None = None, as_json: bool = False) -> str:
    """What this project manages (record_types, counts, statuses) + the verb
    cheatsheet (which verb moves which record where, with its gate)."""
    cfg = _cfg(root)
    d = rec.info(cfg)
    if as_json:
        return json.dumps(d, indent=2, ensure_ascii=False)
    return rec.info_text(d).rstrip()


def proj_changelog_add(entry: str, version: str | None = None, root: str | None = None) -> str:
    """Add a CHANGELOG.md entry: 'Category: text' (Added/Changed/Fixed/…; plain
    text ok). version: default the Unreleased placeholder."""
    cfg = _cfg(root)
    try:
        cl.add(cfg, entry, version)
    except ValueError as e:
        return f"error: {e}"
    return "Added entry"


def proj_changelog_version(name: str | None = None, bump: str | None = None,
                           root: str | None = None) -> str:
    """Release a version: move Unreleased into a version (name: the version,
    e.g. v0.1.0; bump: major | minor | patch from the latest). Exactly one."""
    cfg = _cfg(root)
    try:
        used = cl.version_bump(cfg, name, bump)
    except ValueError as e:
        return f"error: {e}"
    return f"Released {used}"


def proj_changelog_release_notes(version: str, root: str | None = None) -> str:
    """Print one version's CHANGELOG section (for PR/tag bodies)."""
    cfg = _cfg(root)
    try:
        return cl.release_notes(cfg, version).rstrip()
    except ValueError as e:
        return f"error: {e}"


def proj_changelog_list(root: str | None = None) -> str:
    """List released versions (newest first): 'vX.Y.Z  date'."""
    cfg = _cfg(root)
    return "\n".join(f"{v}  {d}" for v, d in cl.show_versions(cfg)) or "(no versions)"


def proj_task_move(id: str, parent: str | None = None, detach: bool = False,
                   root: str | None = None) -> str:
    """Re-home an existing task: parent = the new parent record id (any type),
    detach = True to remove the parent edge entirely (the task becomes
    Unparented). Exactly one of parent/detach. Refused when the target is the
    task itself, one of its own descendants (cycle), or a closed record."""
    cfg = _cfg(root)
    try:
        if bool(parent) == detach:
            return "error: pass exactly one of parent (id) or detach=true"
        row = taskmod.move(cfg, id, parent)
        return f"{row['id']}: parent → {parent if parent else 'none (Unparented)'}"
    except ValueError as e:
        return f"error: {e}"


# ---- items (record checklists): the engine in engine/items.py validates refs,
# unsynced md edits, closed-record re-open and due dates; on success it re-renders
# and returns the record's fresh item list. item ref format: ID#nnn (T-000001#002
# ungrouped; T-000001#A01 inside group A — the letter is never reused).

def _item_call(fn, *args, **kwargs) -> str:
    """Shared body for the proj_item_* tools: config + ValueError → 'error:' text.
    `root` travels in kwargs (like every other tool arg). `ls` returns a list
    (joined here); the other ops return a str (the record's fresh item list)."""
    try:
        result = fn(_cfg(kwargs.pop("root", None)), *args, **kwargs)
        return "\n".join(result) if isinstance(result, list) else result.rstrip()
    except (ValueError, ProjError) as e:
        return f"error: {e}"


def proj_item_ls(ref: str | None = None, group: str | None = None, open_only: bool = False,
                 record_type: str | None = None, owner: str | None = None,
                 overdue: bool = False, as_json: bool = False,
                 root: str | None = None) -> str:
    """List items as 'ID#A01  state  text  [owner]  [due]'. ref: one record id
    (omit = every record); group / open_only / record_type / owner / overdue
    filter. as_json: a JSON array of objects (ref, record, no, group, state,
    closed, text, note, owner, due)."""
    if as_json:
        try:
            rows = item_ops.ls_rows(_cfg(root), ref, group=group, open_only=open_only,
                                    record_type=record_type, owner=owner, overdue=overdue)
        except (ValueError, ProjError) as e:
            return f"error: {e}"
        return json.dumps(rows, indent=2, ensure_ascii=False)
    lines = _item_call(item_ops.ls, ref, group=group, open_only=open_only,
                       record_type=record_type, owner=owner, overdue=overdue, root=root)
    return lines or "(no items)"


def proj_item_add(ref: str, text: str, group: str | None = None, after: str | None = None,
                  state: str | None = None, note: str = "", owner: str = "", due: str = "",
                  root: str | None = None) -> str:
    """Add an item to a record's checklist (auto-numbered, appended to its
    group). group: the group letter L (default the ungrouped list); after:
    insert right after ITEM; state/note/owner/due optional. Returns the fresh
    item list."""
    return _item_call(item_ops.add, ref, text, group=group, after=after, state=state,
                      note=note, owner=owner, due=due, root=root)


def proj_item_set(item: str, state: str, note: str | None = None, commit: str = "",
                  root: str | None = None) -> str:
    """Set an item's state (item = 'ID#n'). note / commit optional. Returns the
    fresh item list. Refused when it would un-close an item on a record in a
    closed (gates.items) status."""
    return _item_call(item_ops.set_state, item, state, note=note, commit=commit, root=root)


def proj_item_check(item: str, note: str | None = None, commit: str = "",
                    root: str | None = None) -> str:
    """Mark an item ('ID#n') closed (first closed state). Returns the fresh item
    list."""
    return _item_call(item_ops.check, item, note=note, commit=commit, root=root)


def proj_item_uncheck(item: str, root: str | None = None) -> str:
    """Re-open an item ('ID#n') (first unclosed state). Returns the fresh item
    list."""
    return _item_call(item_ops.uncheck, item, root=root)


def proj_item_edit(item: str, text: str | None = None, note: str | None = None,
                   owner: str | None = None, due: str | None = None,
                   root: str | None = None) -> str:
    """Edit an item's ('ID#n') text / note / owner / due (empty string clears).
    Returns the fresh item list."""
    return _item_call(item_ops.edit, item, text=text, note=note, owner=owner, due=due,
                      root=root)


def proj_item_mv(item: str, group: str | None = None, ungroup: bool = False,
                 after: str | None = None, first: bool = False,
                 root: str | None = None) -> str:
    """Move an item ('ID#A01'): exactly one of group (into L, fresh number) /
    ungroup (to the ungrouped list, fresh number) / after (right after ITEM) /
    first (top of its group). Cross-group moves get a fresh number. Returns the
    fresh item list."""
    return _item_call(item_ops.move, item, group=group, ungroup=ungroup, after=after,
                      first=first, root=root)


def proj_item_rm(item: str, root: str | None = None) -> str:
    """Remove an item ('ID#A01'); its number is never reused. Returns the fresh
    item list."""
    return _item_call(item_ops.remove, item, root=root)


def proj_item_group_add(ref: str, title: str, root: str | None = None) -> str:
    """Add a group; the tool assigns the next free letter (A-Z, never reused).
    Returns the fresh item list."""
    return _item_call(item_ops.group_add, ref, title, root=root)


def proj_item_group_rename(ref: str, letter: str, title: str, root: str | None = None) -> str:
    """Rename a group. Returns the fresh item list."""
    return _item_call(item_ops.group_rename, ref, letter, title, root=root)


def proj_item_group_rm(ref: str, letter: str, items: str | None = None,
                       root: str | None = None) -> str:
    """Delete a group; `items` = 'ungroup' or 'delete' when it holds items."""
    return _item_call(item_ops.group_rm, ref, letter, items_mode=items, root=root)


def proj_item_group_mv(ref: str, letter: str, after: str | None = None,
                       first: bool = False, root: str | None = None) -> str:
    """Move a group: exactly one of after / first. Returns the fresh item list."""
    return _item_call(item_ops.group_mv, ref, letter, after=after, first=first, root=root)


# ---- D3: register the functions above ONCE (FastMCP accepts sync functions —
# there is no separate async wrapper with a duplicated docstring) ---------------

for _fn in (proj_init, proj_new, proj_sync, proj_adopt, proj_render, proj_check,
            proj_ls, proj_list, proj_show, proj_open, proj_update, proj_verb,
            proj_info,
            proj_changelog_add, proj_changelog_version, proj_changelog_release_notes,
            proj_changelog_list,
            proj_task_move,
            proj_item_ls, proj_item_add, proj_item_set, proj_item_check,
            proj_item_uncheck, proj_item_edit, proj_item_mv, proj_item_rm,
            proj_item_group_add, proj_item_group_rename, proj_item_group_rm,
            proj_item_group_mv):
    mcp.tool(name=_fn.__name__)(_fn)


def main():
    mcp.run()  # default stdio transport


if __name__ == "__main__":
    main()
