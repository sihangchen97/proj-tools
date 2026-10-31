"""MCP server for proj (stdio) — thin wrappers over the same engine functions as the CLI.

Run: `proj-mcp` (registered in pyproject [project.scripts]).
Every tool takes an optional `root` (absolute project path, = the CLI `--root`),
so it works from any cwd. Returns human-readable text; on validation failure the
full error message is returned (agent-readable).
"""
from __future__ import annotations

import json
from pathlib import Path

from mcp.server import MCPServer

from proj import scaffold
from proj.check import check as run_checks
from proj.core.config import load_config
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.record_types import changelog as cl
from proj.record_types import decision as dec
from proj.record_types import plan as planmod
from proj.record_types import task as taskmod

mcp = MCPServer("proj")

PLAN_VERBS = ("activate", "review", "shelve", "resume", "archive", "freeze")


def _cfg(root: str | None, strict: bool = True):
    return load_config(Path(root) if root else None, strict=strict)


def _fmt_rows(rows: list[dict], status: str | None) -> str:
    if not rows:
        return "(no records)" + (f" with status {status!r}" if status else "")
    return "\n".join(f"{r['id']:<8} {r['status']:<24} {r['title']}" for r in rows)


# ---- pure tool bodies (tests/test_mcp.py calls these directly) ----------------

def proj_init(root: str | None = None, profile: str = "core") -> str:
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
             field: str | None = None, summary: str | None = None) -> str:
    """Create a record. supersede: comma-separated decision ids (decision only);
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
    if record_type not in rec.load_registry(cfg.root):
        return (f"error: unknown record_type {record_type!r} "
                f"(run proj_info for registered record_types)")
    try:
        if record_type == "decision":
            flat = [x.strip() for x in (supersede or "").split(",") if x.strip()]
            _, p = dec.new(cfg, title, flat or None, summary or "")
        elif record_type == "task":
            _, p = taskmod.new(cfg, title, parent, summary or "")
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
    if record_type not in rec.load_registry(cfg.root):
        return f"error: unknown record_type {record_type!r}"
    return _fmt_rows(record_for(record_type, rec.load_registry(cfg.root)).list(cfg, status),
                     status)


def proj_show(record_type: str, id_: str, root: str | None = None,
              history: bool = False) -> str:
    cfg = _cfg(root)
    reg = rec.load_registry(cfg.root)
    if record_type not in reg:
        return f"error: unknown record_type {record_type!r}"
    spec = reg[record_type]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        return f"error: {record_type} {id_} not found"
    lines = [f"id: {row['id']}", f"uuid: {row['uuid']}", f"title: {row['title']}",
             f"status: {row['status']}", f"created: {row['created']}"]
    for f in spec.fields:
        if row.get(f.name):
            lines.append(f"{f.name}: {row[f.name]}")
    if history:
        for e in db.logs(record_type, row["uuid"]):
            lines.append(f"  {e['date']}  [{e['category']}] {e['summary']}")
    db.close()
    return "\n".join(lines)


def proj_status(record_type: str, id_: str, new_status: str, root: str | None = None) -> str:
    """Set a record's status (validated against the template's allowed set)."""
    cfg = _cfg(root)
    if record_type not in rec.load_registry(cfg.root):
        return f"error: unknown record_type {record_type!r}"
    try:
        row = record_for(record_type, rec.load_registry(cfg.root)).set_status(cfg, id_, new_status)
    except ValueError as e:
        return f"error: {e}"
    return f"{record_type} {row['id']}: status → {new_status}"


def proj_log(record_type: str, id_: str, summary: str, root: str | None = None) -> str:
    """Append a changelog entry: 'Category: one-line summary' (or plain text)."""
    cfg = _cfg(root)
    if record_type not in rec.load_registry(cfg.root):
        return f"error: unknown record_type {record_type!r}"
    try:
        record_for(record_type, rec.load_registry(cfg.root)).add_log(cfg, id_, summary)
    except ValueError as e:
        return f"error: {e}"
    return f"logged on {record_type} {id_}"


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


def proj_task_done(id_: str, root: str | None = None) -> str:
    """Mark a task Done (requires every step + sub-task checked)."""
    cfg = _cfg(root)
    try:
        row = taskmod.done(cfg, id_)
    except ValueError as e:
        return f"error: {e}"
    return f"task {row['id']} → Done"


def proj_plan_verb(verb: str, id_: str, root: str | None = None) -> str:
    """Drive a plan's state machine: activate|review|shelve|resume|archive|freeze."""
    if verb not in PLAN_VERBS:
        return f"error: plan verb must be one of {', '.join(PLAN_VERBS)}, got {verb!r}"
    cfg = _cfg(root)
    try:
        row = planmod.transition(cfg, verb, id_)
    except ValueError as e:
        return f"error: {e}"
    return f"plan {row['id']}: status → {row['status']}"


def proj_decision_supersede(title: str, supersede: str, root: str | None = None,
                           summary: str | None = None) -> str:
    """Create a decision that supersedes others. supersede: comma-separated ids."""
    cfg = _cfg(root)
    flat = [x.strip() for x in (supersede or "").split(",") if x.strip()]
    try:
        _, p = dec.new(cfg, title, flat or None, summary or "")
    except ValueError as e:
        return f"error: {e}"
    return f"Created {p.relative_to(cfg.root)}"


def proj_changelog_add(entry: str, version: str | None = None, root: str | None = None) -> str:
    """Add an entry: 'Category: text' (Added/Changed/Fixed/…; plain text ok)."""
    cfg = _cfg(root)
    try:
        cl.add(cfg, entry, version)
    except ValueError as e:
        return f"error: {e}"
    return "Added entry"


# ---- MCP tools (thin async wrappers; root = CLI --root) ------------------------

@mcp.tool()
async def proj_init_tool(root: str | None = None, profile: str = "core") -> str:
    """Scaffold a new project: files, DB tables (lock wins over templates), seed, adopt.
    root: absolute project path (default: discover by walking up from cwd).
    profile: core, cv, infra or quality (repeat calls to add profiles)."""
    return proj_init(root, profile)


@mcp.tool()
async def proj_new_tool(record_type: str, title: str, root: str | None = None,
                        supersede: str | None = None, parent: str | None = None,
                        field: str | None = None, summary: str | None = None) -> str:
    """Create a record from its template (DB insert + rendered md).
    record_type: registered type (decision/task/plan/debt/incident/experiment/…).
    supersede: decision ids being replaced, comma-separated (decision only).
    parent: parent record id, any type (task only). field: extra template fields,
    'name=value[, name=value]'. summary: one-sentence summary."""
    return proj_new(record_type, title, root, supersede, parent, field, summary)


@mcp.tool()
async def proj_sync_tool(files: str | None = None, root: str | None = None) -> str:
    """md → DB (validate, normalize) → re-render all md. files: comma-separated
    paths (default: all managed dirs). FAIL lines mean the DB was not fully updated."""
    return proj_sync(files, root)


@mcp.tool()
async def proj_check_tool(root: str | None = None) -> str:
    """Validate DB ↔ lock ↔ templates ↔ md invariants. 'OK' or FAIL lines."""
    return proj_check(root)


@mcp.tool()
async def proj_list_tool(record_type: str, status: str | None = None,
                        root: str | None = None) -> str:
    """List records of a record_type (from DB), optionally filtered by status."""
    return proj_list(record_type, status, root)


@mcp.tool()
async def proj_show_tool(record_type: str, id_: str, root: str | None = None,
                        history: bool = False) -> str:
    """Show one record (from DB); history: append its full changelog."""
    return proj_show(record_type, id_, root, history)


@mcp.tool()
async def proj_status_tool(record_type: str, id_: str, new_status: str,
                          root: str | None = None) -> str:
    """Set a record's status (validated against the template's allowed set)."""
    return proj_status(record_type, id_, new_status, root)


@mcp.tool()
async def proj_log_tool(record_type: str, id_: str, summary: str,
                       root: str | None = None) -> str:
    """Append a changelog entry: 'Category: one-line summary' (or plain text)."""
    return proj_log(record_type, id_, summary, root)


@mcp.tool()
async def proj_update_tool(root: str | None = None) -> str:
    """sync all + re-render every view (TASKLIST.md, CHANGELOG.md, view sections)."""
    return proj_update(root)


@mcp.tool()
async def proj_info_tool(root: str | None = None, as_json: bool = False) -> str:
    """What this project manages (record_types, counts, statuses) + verb cheatsheet."""
    return proj_info(root, as_json)


@mcp.tool()
async def proj_task_done_tool(id_: str, root: str | None = None) -> str:
    """Mark a task Done (requires every step + sub-task checked)."""
    return proj_task_done(id_, root)


@mcp.tool()
async def proj_plan_verb_tool(verb: str, id_: str, root: str | None = None) -> str:
    """Drive a plan's state machine: activate (Draft→Active), review (Active→Pending
    review, requires all its tasks Done), shelve (Pending review→Shelved), resume
    (Shelved→Active), archive (Pending review→Archived), freeze (Shelved→Archived)."""
    return proj_plan_verb(verb, id_, root)


@mcp.tool()
async def proj_decision_supersede_tool(title: str, supersede: str, root: str | None = None,
                                       summary: str | None = None) -> str:
    """Create a decision that supersedes existing ones (sets supersedes + appends
    to each old one's superseded_by). supersede: comma-separated decision ids."""
    return proj_decision_supersede(title, supersede, root, summary)


@mcp.tool()
async def proj_changelog_add_tool(entry: str, version: str | None = None,
                                  root: str | None = None) -> str:
    """Add a CHANGELOG.md entry: 'Category: text' (Added/Changed/Fixed/…; plain
    text ok). version: default the Unreleased placeholder."""
    return proj_changelog_add(entry, version, root)


def main():
    mcp.run()  # default stdio transport


if __name__ == "__main__":
    main()
