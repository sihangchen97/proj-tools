"""MCP server for proj (stdio) — thin wrappers over the same engine functions as the CLI.

Run: `proj-mcp` (registered in pyproject [project.scripts]).
Every tool takes an optional `root` (absolute project path, = the CLI `--root`);
default = the current directory (no walking up). Returns human-readable text;
on validation failure the full error message is returned (agent-readable).

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
from proj.core.db import show_date
from proj.core.templates import resolve_record_type
from proj.engine import item_ops
from proj.engine import items as items_mod
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
    """Run a pure tool body; config-discovery failures become clean 'error:' text."""
    try:
        return fn(*args)
    except ValueError as e:
        return f"error: {e}"


def _rtype(record_type: str, reg: dict) -> str | None:
    """Canonical record_type (name or code, case-insensitive); None if unknown."""
    try:
        return resolve_record_type(record_type, reg)
    except ValueError:
        return None


def _fmt_rows(rows: list[dict], status: str | None) -> str:
    if not rows:
        return "(no records)" + (f" with status {status!r}" if status else "")
    return "\n".join(f"{r['id']:<8} {r['status']:<24} {r['title']}" for r in rows)


# ---- pure tool bodies (tests/test_mcp.py calls these directly) ----------------

def proj_init(root: str | None = None, profile: str = "default") -> str:
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
    """Create a record. supersede: comma-separated ids being replaced, same type;
    related: comma-separated record ids, any type (unordered);
    parent: parent record id, any type (task only); commit: git commit hash."""
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return ("error: unknown record_type — name or code, case-insensitive "
                "(run proj_info for registered record_types)")
    flat_sup = [x.strip() for x in (supersede or "").split(",") if x.strip()]
    flat_rel = [x.strip() for x in (related or "").split(",") if x.strip()]
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
    """md → DB (validate, normalize) → re-render all md. files: comma-separated paths."""
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


def proj_check(root: str | None = None) -> str:
    cfg = _cfg(root)
    problems = run_checks(cfg)
    if problems:
        return "\n".join(f"FAIL  {p}" for p in problems)
    return "OK"


def proj_list(record_type: str, status: str | None = None, root: str | None = None) -> str:
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return "error: unknown record_type (name or code, case-insensitive)"
    return _fmt_rows(record_for(record_type, reg).list(cfg, status), status)


def proj_show(record_type: str, id_: str, root: str | None = None,
              history: bool = False) -> str:
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return "error: unknown record_type (name or code, case-insensitive)"
    spec = reg[record_type]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        return f"error: {record_type} {id_} not found"
    lines = [f"id: {row['id']}", f"title: {row['title']}",
             f"status: {row['status']}", f"created: {show_date(row['created'])}"]
    if row.get("one_sentence"):
        lines.append(f"one_sentence: {row['one_sentence']}")
    if row.get("commit"):
        lines.append(f"commit: {row['commit']}")
    if spec.record_type == "task":
        parent = db.parent_of(row["id"])
        if parent:
            lines.append(f"parent: {parent['source']}")
    if spec.items is not None:
        intro = json.loads(row["sections"] or "{}").get(spec.items.section.lower(), "")
        rendered = items_mod.render_section(
            items_mod.load(db, row["id"], intro=intro), spec)
        if rendered:
            lines.append("")
            lines.append(f"{spec.items.section}:")
            lines.append(rendered)
    if history:
        for e in db.logs(row["id"]):
            lines.append(f"  {show_date(e['date'])}  [{e['category']}] "
                         f"{rec._log_body(e, spec)}")
    db.close()
    return "\n".join(lines)


def proj_update_record(record_type: str, id_: str, title: str | None = None,
                       summary: str | None = None, message: str | None = None,
                       root: str | None = None) -> str:
    """Update a record's title / summary. message: optionally append a changelog
    entry ('Category: text'). title change also renames the md file (id-slug.md)."""
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return "error: unknown record_type (name or code, case-insensitive)"
    if title is None and summary is None and not message:
        return "error: nothing to update — pass title / summary / message"
    try:
        row = rec.update_record(cfg, record_type, id_,
                                title=title, summary=summary, message=message)
    except ValueError as e:
        return f"error: {e}"
    return f"{record_type} {row['id']}: title → {row['title']}"


def proj_verb(record_type: str, verb: str, id_: str, title: str | None = None,
              summary: str | None = None, message: str | None = None,
              root: str | None = None) -> str:
    """Run a template verb (a state-machine step) on a record.
    verb must be one the record_type's template declares (e.g. decision accept,
    plan activate, task done). `from: Any` verbs work from any status.
    title/summary/message: optionally also update them after the step
    (message appends a changelog entry, 'Category: text')."""
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return "error: unknown record_type (name or code, case-insensitive)"
    try:
        row = record_for(record_type, reg).transition(cfg, verb, id_)
        if title is not None or summary is not None or message:
            row = rec.update_record(cfg, record_type, row["id"],
                                    title=title, summary=summary, message=message)
    except ValueError as e:
        return f"error: {e}"
    return f"{record_type} {row['id']}: status → {row['status']}"


def proj_update(root: str | None = None) -> str:
    """sync all + re-render every view (TASKLIST.md, CHANGELOG.md, view sections)."""
    cfg = _cfg(root)
    problems, notes = rec.sync(cfg, None)
    out = [f"FAIL  {p}" for p in problems] + [f"note: {n}" for n in notes]
    if problems:
        return "\n".join(out)
    return "OK — updated"


def proj_info(root: str | None = None, as_json: bool = False) -> str:
    """What this directory manages + which verbs change it (for humans and MCP)."""
    cfg = _cfg(root)
    d = rec.info(cfg)
    if as_json:
        return json.dumps(d, indent=2, ensure_ascii=False)
    return rec.info_text(d).rstrip()


def proj_changelog_add(entry: str, version: str | None = None, root: str | None = None) -> str:
    """Add an entry: 'Category: text' (Added/Changed/Fixed/…; plain text ok)."""
    cfg = _cfg(root)
    try:
        cl.add(cfg, entry, version)
    except ValueError as e:
        return f"error: {e}"
    return "Added entry"


def proj_task_move(id_: str, parent: str | None = None, detach: bool = False,
                   root: str | None = None) -> str:
    """Re-home an existing task: parent = the new parent record id (any type),
    detach = True to remove the parent edge entirely (the task becomes Unparented).
    Exactly one of parent/detach. Refused when the target is the task itself, one
    of its own descendants (cycle), or a closed record (gates.tasks state)."""
    cfg = _cfg(root)
    try:
        if bool(parent) == detach:
            return "error: pass exactly one of parent (id) or detach=true"
        row = taskmod.move(cfg, id_, parent)
        return f"{row['id']}: parent → {parent if parent else 'none (Unparented)'}"
    except ValueError as e:
        return f"error: {e}"


# ---- items (record checklists): the engine in engine/items.py validates refs,
# unsynced md edits, closed-record re-open and due dates; on success it re-renders
# and returns the record's fresh item list. item ref format: ID#nnn (T-000001#002
# ungrouped; T-000001#A01 inside group A — the letter is never reused).

def _item_call(fn, *args, **kwargs) -> str:
    """Shared body for the proj_item_* tools: config + ValueError → 'error:' text.
    `root` travels in kwargs (like every other tool arg). `ls` returns a list (joined
    here); the other ops return a str (the record's fresh item list)."""
    try:
        result = fn(_cfg(kwargs.pop("root", None)), *args, **kwargs)
        return "\n".join(result) if isinstance(result, list) else result.rstrip()
    except ValueError as e:
        return f"error: {e}"


def proj_item_ls(ref: str | None = None, group: str | None = None, open_only: bool = False,
                 record_type: str | None = None, owner: str | None = None,
                 overdue: bool = False, root: str | None = None) -> str:
    """List items as 'ID#n  state  text  [owner]  [due]'. ref: one record id;
    omitted = every record. open_only: only unclosed items. record_type/owner/
    overdue: filters (overdue = past-due and still unclosed)."""
    lines = _item_call(item_ops.ls, ref, group=group, open_only=open_only,
                       record_type=record_type, owner=owner, overdue=overdue, root=root)
    return lines or "(no items)"


def proj_item_add(ref: str, text: str, group: str | None = None, after: str | None = None,
                  state: str | None = None, note: str = "", owner: str = "", due: str = "",
                  root: str | None = None) -> str:
    """Add an item to a record's checklist (auto-numbered; appended at the end of
    its group). ref: the record id. group: add to group L (default ungrouped).
    after: insert right after ITEM. state: start in a non-default state.
    note/owner/due: optional fields (owner/due only where the type enables them).
    Returns the record's fresh item list."""
    return _item_call(item_ops.add, ref, text, group=group, after=after, state=state,
                      note=note, owner=owner, due=due, root=root)


def proj_item_set(item: str, state: str, note: str | None = None, commit: str = "",
                  root: str | None = None) -> str:
    """Set an item's state (item = 'ID#n', state = one of the type's checklist
    states). note: also set the note; commit: a git commit hash. Returns the
    record's fresh item list. Refused when it would un-close an item on a record
    in a closed (gates.items) status."""
    return _item_call(item_ops.set_state, item, state, note=note, commit=commit, root=root)


def proj_item_check(item: str, note: str | None = None, commit: str = "",
                    root: str | None = None) -> str:
    """Mark an item ('ID#n') closed — the first closed state of its checklist.
    Returns the record's fresh item list."""
    return _item_call(item_ops.check, item, note=note, commit=commit, root=root)


def proj_item_uncheck(item: str, root: str | None = None) -> str:
    """Re-open an item ('ID#n') — back to the first, unclosed state. Refused on a
    record in a closed (gates.items) status. Returns the fresh item list."""
    return _item_call(item_ops.uncheck, item, root=root)


def proj_item_edit(item: str, text: str | None = None, note: str | None = None,
                   owner: str | None = None, due: str | None = None,
                   root: str | None = None) -> str:
    """Edit an item's ('ID#n') text / note / owner / due; an empty string clears
    a field. Returns the record's fresh item list."""
    return _item_call(item_ops.edit, item, text=text, note=note, owner=owner, due=due,
                      root=root)


def proj_item_mv(item: str, group: str | None = None, ungroup: bool = False,
                 after: str | None = None, first: bool = False,
                 root: str | None = None) -> str:
    """Move an item ('ID#A01'): exactly one of group (into L, fresh number) /
    ungroup (to the ungrouped list, fresh number) / after (right after ITEM) /
    first (to the top of its group). Cross-group moves get a fresh number (the
    old one is void). Returns the fresh item list."""
    return _item_call(item_ops.move, item, group=group, ungroup=ungroup, after=after,
                      first=first, root=root)


def proj_item_rm(item: str, root: str | None = None) -> str:
    """Remove an item ('ID#A01'); its number is never reused. Returns the fresh
    item list."""
    return _item_call(item_ops.remove, item, root=root)


def proj_item_group_add(ref: str, title: str, root: str | None = None) -> str:
    """Add a group to a record's checklist; the tool assigns the next free letter
    (A-Z, never reused). Returns the fresh item list."""
    return _item_call(item_ops.group_add, ref, title, root=root)


def proj_item_group_rename(ref: str, letter: str, title: str,
                           root: str | None = None) -> str:
    """Rename a group (letters are stable; only the name changes). Returns the
    fresh item list."""
    return _item_call(item_ops.group_rename, ref, letter, title, root=root)


def proj_item_group_rm(ref: str, letter: str, items: str | None = None,
                       root: str | None = None) -> str:
    """Delete a group. When it still holds items, `items` must be 'ungroup'
    (fresh ungrouped numbers) or 'delete'."""
    return _item_call(item_ops.group_rm, ref, letter, items_mode=items, root=root)


def proj_item_group_mv(ref: str, letter: str, after: str | None = None,
                       first: bool = False, root: str | None = None) -> str:
    """Move a group (its items keep their numbers): exactly one of after (group
    B) / first. Returns the fresh item list."""
    return _item_call(item_ops.group_mv, ref, letter, after=after, first=first, root=root)


# ---- MCP tools (thin async wrappers; exposed tool names match the CLI verbs) ----

@mcp.tool(name="proj_init")
async def _t_init(root: str | None = None, profile: str = "default") -> str:
    """Scaffold a new project: files, DB tables (lock wins over templates), seed, adopt.
    root: absolute project path (default: the current directory).
    profile: default plus additive names (cv, infra, quality, ...); the choice
    is pinned in .proj.lock — repeat inits append new profiles."""
    return _safe(proj_init, root, profile)


@mcp.tool(name="proj_new")
async def _t_new(record_type: str, title: str, root: str | None = None,
                 supersede: str | None = None, parent: str | None = None,
                 related: str | None = None, commit: str | None = None,
                 summary: str | None = None) -> str:
    """Create a record from its template (DB insert + rendered md).
    record_type: registered type (decision/task/plan/debt/incident/experiment/…).
    supersede: ids being replaced, same type, comma-separated.
    related: related record ids, any type, comma-separated (unordered).
    parent: parent record id (task only). commit: git commit hash.
    summary: one-sentence summary."""
    return _safe(proj_new, record_type, title, root, supersede, parent, related,
                 commit, summary)


@mcp.tool(name="proj_sync")
async def _t_sync(files: str | None = None, root: str | None = None) -> str:
    """md → DB (validate, normalize) → re-render all md. files: comma-separated
    paths (default: all managed dirs). FAIL lines mean the DB was not fully updated."""
    return _safe(proj_sync, files, root)


@mcp.tool(name="proj_check")
async def _t_check(root: str | None = None) -> str:
    """Validate DB ↔ lock ↔ templates ↔ md invariants. 'OK' or FAIL lines."""
    return _safe(proj_check, root)


@mcp.tool(name="proj_list")
async def _t_list(record_type: str, status: str | None = None,
                  root: str | None = None) -> str:
    """List records of a record_type (from DB), optionally filtered by status."""
    return _safe(proj_list, record_type, status, root)


@mcp.tool(name="proj_show")
async def _t_show(record_type: str, id_: str, root: str | None = None,
                  history: bool = False) -> str:
    """Show one record (from DB); history: append its full changelog."""
    return _safe(proj_show, record_type, id_, root, history)


@mcp.tool(name="proj_verb")
async def _t_verb(record_type: str, verb: str, id_: str, title: str | None = None,
                  summary: str | None = None, message: str | None = None,
                  root: str | None = None) -> str:
    """Run a template verb (a state-machine step) on a record, e.g.
    `('decision','accept','DEC-0002')`, `('plan','activate','PLN-0001')`,
    `('task','done','T-000001')`. verb must be one the record_type's template
    declares; title/summary/message optionally also update it after the step."""
    return _safe(proj_verb, record_type, verb, id_, title, summary, message, root)


@mcp.tool(name="proj_update_record")
async def _t_update_record(record_type: str, id_: str, title: str | None = None,
                           summary: str | None = None, message: str | None = None,
                           root: str | None = None) -> str:
    """Update a record's title / summary (a title change renames its md file);
    message: optionally append a changelog entry ('Category: text')."""
    return _safe(proj_update_record, record_type, id_, title, summary, message, root)


@mcp.tool(name="proj_update")
async def _t_update(root: str | None = None) -> str:
    """sync all + re-render every view (TASKLIST.md, CHANGELOG.md, view sections)."""
    return _safe(proj_update, root)


@mcp.tool(name="proj_info")
async def _t_info(root: str | None = None, as_json: bool = False) -> str:
    """What this project manages (record_types, counts, statuses) + verb cheatsheet."""
    return _safe(proj_info, root, as_json)


@mcp.tool(name="proj_changelog_add")
async def _t_changelog_add(entry: str, version: str | None = None,
                           root: str | None = None) -> str:
    """Add a CHANGELOG.md entry: 'Category: text' (Added/Changed/Fixed/…; plain
    text ok). version: default the Unreleased placeholder."""
    return _safe(proj_changelog_add, entry, version, root)


@mcp.tool(name="proj_task_move")
async def _t_task_move(id_: str, parent: str | None = None, detach: bool = False,
                       root: str | None = None) -> str:
    """Move an existing task under a (possibly new) parent record, or detach it.
    parent: new parent id (any record type); detach: True to unparent. Exactly
    one of the two. Refused for itself / its own descendants / closed records."""
    return _safe(proj_task_move, id_, parent, detach, root)


@mcp.tool(name="proj_item_ls")
async def _t_item_ls(ref: str | None = None, group: str | None = None,
                     open_only: bool = False, record_type: str | None = None,
                     owner: str | None = None, overdue: bool = False,
                     root: str | None = None) -> str:
    """List items as 'ID#A01  state  text  [owner]  [due]'. ref: one record id
    (omit = every record); group / open_only / record_type / owner / overdue
    filter."""
    return _safe(proj_item_ls, ref, group, open_only, record_type, owner, overdue, root)


@mcp.tool(name="proj_item_add")
async def _t_item_add(ref: str, text: str, group: str | None = None,
                      after: str | None = None, state: str | None = None,
                      note: str = "", owner: str = "", due: str = "",
                      root: str | None = None) -> str:
    """Add an item to a record's checklist (auto-numbered, appended to its
    group). group: the group letter L (default the ungrouped list); after:
    insert right after ITEM; state/note/owner/due optional. Returns the fresh
    item list."""
    return _safe(proj_item_add, ref, text, group, after, state, note, owner, due, root)


@mcp.tool(name="proj_item_set")
async def _t_item_set(item: str, state: str, note: str | None = None,
                      commit: str = "", root: str | None = None) -> str:
    """Set an item's state (item = 'ID#n'). note / commit optional. Returns the
    fresh item list."""
    return _safe(proj_item_set, item, state, note, commit, root)


@mcp.tool(name="proj_item_check")
async def _t_item_check(item: str, note: str | None = None, commit: str = "",
                        root: str | None = None) -> str:
    """Mark an item ('ID#n') closed (first closed state). Returns the fresh item
    list."""
    return _safe(proj_item_check, item, note, commit, root)


@mcp.tool(name="proj_item_uncheck")
async def _t_item_uncheck(item: str, root: str | None = None) -> str:
    """Re-open an item ('ID#n') (first unclosed state). Returns the fresh item
    list."""
    return _safe(proj_item_uncheck, item, root)


@mcp.tool(name="proj_item_edit")
async def _t_item_edit(item: str, text: str | None = None, note: str | None = None,
                       owner: str | None = None, due: str | None = None,
                       root: str | None = None) -> str:
    """Edit an item's ('ID#n') text / note / owner / due (empty string clears).
    Returns the fresh item list."""
    return _safe(proj_item_edit, item, text, note, owner, due, root)


@mcp.tool(name="proj_item_mv")
async def _t_item_mv(item: str, group: str | None = None, ungroup: bool = False,
                     after: str | None = None, first: bool = False,
                     root: str | None = None) -> str:
    """Move an item ('ID#A01'): exactly one of group (into L, fresh number) /
    ungroup (to the ungrouped list, fresh number) / after (right after ITEM) /
    first (top of its group). Cross-group moves get a fresh number. Returns the
    fresh item list."""
    return _safe(proj_item_mv, item, group, ungroup, after, first, root)


@mcp.tool(name="proj_item_rm")
async def _t_item_rm(item: str, root: str | None = None) -> str:
    """Remove an item ('ID#A01'); its number is never reused. Returns the fresh
    item list."""
    return _safe(proj_item_rm, item, root)


@mcp.tool(name="proj_item_group_add")
async def _t_item_group_add(ref: str, title: str, root: str | None = None) -> str:
    """Add a group; the tool assigns the next free letter (A-Z, never reused).
    Returns the fresh item list."""
    return _safe(proj_item_group_add, ref, title, root)


@mcp.tool(name="proj_item_group_rename")
async def _t_item_group_rename(ref: str, letter: str, title: str,
                               root: str | None = None) -> str:
    """Rename a group. Returns the fresh item list."""
    return _safe(proj_item_group_rename, ref, letter, title, root)


@mcp.tool(name="proj_item_group_rm")
async def _t_item_group_rm(ref: str, letter: str, items: str | None = None,
                           root: str | None = None) -> str:
    """Delete a group; `items` = 'ungroup' or 'delete' when it holds items."""
    return _safe(proj_item_group_rm, ref, letter, items, root)


@mcp.tool(name="proj_item_group_mv")
async def _t_item_group_mv(ref: str, letter: str, after: str | None = None,
                           first: bool = False, root: str | None = None) -> str:
    """Move a group: exactly one of after / first. Returns the fresh item list."""
    return _safe(proj_item_group_mv, ref, letter, after, first, root)


def main():
    mcp.run()  # default stdio transport


if __name__ == "__main__":
    main()
