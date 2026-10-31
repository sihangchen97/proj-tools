"""MCP server for proj (stdio) — thin wrappers over the same engine functions as the CLI.

Run: `proj-mcp` (registered in pyproject [project.scripts]).
Every tool takes an optional `root` (absolute project path, = the CLI `--root`);
default = the current directory (no walking up). Returns human-readable text;
on validation failure the full error message is returned (agent-readable).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server import MCPServer

from proj import scaffold
from proj.check import check as run_checks
from proj.core.config import load_config
from proj.core.templates import resolve_record_type
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
        return load_config(Path(root) if root else None, strict=strict)
    except FileNotFoundError as e:
        raise ValueError(str(e)) from e


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
             related: str | None = None, field: str | None = None,
             summary: str | None = None) -> str:
    """Create a record. supersede: comma-separated decision ids (decision only);
    related: comma-separated record ids, any type (unordered);
    parent: parent record id, any type (task only); field: comma-separated name=value."""
    cfg = _cfg(root)
    fields = {}
    for f in (field or "").split(","):
        if not f.strip():
            continue
        if "=" not in f:
            return f"error: field expects name=value, got {f!r}"
        k, v = f.split("=", 1)
        fields[k.strip()] = v.strip()
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return ("error: unknown record_type — name or code, case-insensitive "
                "(run proj_info for registered record_types)")
    flat_sup = [x.strip() for x in (supersede or "").split(",") if x.strip()]
    flat_rel = [x.strip() for x in (related or "").split(",") if x.strip()]
    try:
        if record_type == "task":
            _, p = taskmod.new(cfg, title, parent, summary or "", flat_rel or None)
        elif flat_sup or flat_rel:
            obj = record_for(record_type, reg)
            _, p = obj.new(cfg, title, fields=fields or None, summary=summary or "",
                           supersede=flat_sup or None, related=flat_rel or None)
        else:
            _, p = rec.new_record(cfg, record_type, title,
                                  fields=fields or None, summary=summary or "")
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
             f"status: {row['status']}", f"created: {row['created']}"]
    for f in spec.fields:
        if row.get(f.name):
            lines.append(f"{f.name}: {row[f.name]}")
    if history:
        for e in db.logs(record_type, row["id"]):
            lines.append(f"  {e['date']}  [{e['category']}] {e['summary']}")
    db.close()
    return "\n".join(lines)


def proj_update_record(record_type: str, id_: str, title: str | None = None,
                       summary: str | None = None, comment: str | None = None,
                       root: str | None = None) -> str:
    """Update a record's title / summary. comment: optionally append a changelog
    entry ('Category: text'). title change also renames the md file (id-slug.md)."""
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return "error: unknown record_type (name or code, case-insensitive)"
    if title is None and summary is None and not comment:
        return "error: nothing to update — pass title / summary / comment"
    try:
        row = rec.update_record(cfg, record_type, id_,
                                title=title, summary=summary, comment=comment)
    except ValueError as e:
        return f"error: {e}"
    return f"{record_type} {row['id']}: title → {row['title']}"


def proj_verb(record_type: str, verb: str, id_: str, title: str | None = None,
              summary: str | None = None, comment: str | None = None,
              root: str | None = None) -> str:
    """Run a template verb (a state-machine step) on a record.
    verb must be one the record_type's template declares (e.g. decision accept,
    plan activate, task done). `from: Any` verbs work from any status.
    title/summary/comment: optionally also update them after the step
    (comment appends a changelog entry, 'Category: text')."""
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    record_type = _rtype(record_type, reg)
    if record_type is None:
        return "error: unknown record_type (name or code, case-insensitive)"
    try:
        row = record_for(record_type, reg).transition(cfg, verb, id_)
        if title is not None or summary is not None or comment:
            row = rec.update_record(cfg, record_type, row["id"],
                                    title=title, summary=summary, comment=comment)
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
                 related: str | None = None, field: str | None = None,
                 summary: str | None = None) -> str:
    """Create a record from its template (DB insert + rendered md).
    record_type: registered type (decision/task/plan/debt/incident/experiment/…).
    supersede: ids being replaced, comma-separated (decision only).
    related: related record ids, any type, comma-separated (unordered).
    parent: parent record id (task only). field: extra template fields,
    'name=value[, name=value]'. summary: one-sentence summary."""
    return _safe(proj_new, record_type, title, root, supersede, parent, related, field, summary)


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
                  summary: str | None = None, comment: str | None = None,
                  root: str | None = None) -> str:
    """Run a template verb (a state-machine step) on a record, e.g.
    `('decision','accept','DEC-0002')`, `('plan','activate','PLN-0001')`,
    `('task','done','T-000001')`. verb must be one the record_type's template
    declares; title/summary/comment optionally also update it after the step."""
    return _safe(proj_verb, record_type, verb, id_, title, summary, comment, root)


@mcp.tool(name="proj_update_record")
async def _t_update_record(record_type: str, id_: str, title: str | None = None,
                           summary: str | None = None, comment: str | None = None,
                           root: str | None = None) -> str:
    """Update a record's title / summary (a title change renames its md file);
    comment: optionally append a changelog entry ('Category: text')."""
    return _safe(proj_update_record, record_type, id_, title, summary, comment, root)


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


def main():
    mcp.run()  # default stdio transport


if __name__ == "__main__":
    main()
