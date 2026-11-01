"""WIP.md (plans §5) — the project-root "what is being worked on" list.

Membership is REGISTERED (the ``wip_entries`` table), not derived: any record
type may sit in it, added manually (``proj wip add``) or automatically when a
record switches into a status listed in its type's ``wip.enter``. The file is
tool-owned except the item lines (and their ``###`` group headings) — those are
edit in place and ``proj sync`` writes them back into the record (``items`` is
the only writer of the checklist rows).

The per-entry item section is rendered by ``items.render_section`` — byte-for-byte
the same shape as the record md's checklist — so sync can hand it back to
``items.sync_section`` (see ``wip_sync``).
"""
from __future__ import annotations

import os
from pathlib import Path

from proj.core.config import ProjConfig
from proj.core.templates import RecordTypeSpec
from proj.engine.views import View

WIP_FILE = "WIP.md"
ENTRY_MARK = "<!-- proj:wip-entry {id} -->"
WIP_HEADER = ("<!-- proj:wip — TOOL-MANAGED except item lines and group "
              "headings; edit items here, `proj sync` writes them back -->")
FOOTER = ("_Rendered by `proj update` — never hand-edit the headings, metadata "
          "and summary; the item lines are yours to tick and `proj sync` writes "
          "them back. Add/remove with `proj wip add` / `proj wip return`._")


def closed_states(spec: RecordTypeSpec) -> set[str]:
    """The names of a checklist's closed states (the 'done' half)."""
    return {s.name for s in spec.items.states if s.closed} if spec.items else set()


def _record_by_id(db, record_id: str) -> dict | None:
    """A record row straight from the DB (type included) — the registry is keyed
    by record_type, and WIP entries are addressed by id alone."""
    row = db.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def _progress(row: dict, spec: RecordTypeSpec, db) -> str:
    """The metadata line's `n/m`: the checklist's closed/total items; for a task
    WITHOUT checklist items, its terminal/total sub-tasks."""
    from proj.engine import items as items_mod  # lazy: wip↔items cycle
    if spec.items is not None:
        live = items_mod.load(db, row["id"], spec).live()
        items = [i for i in live if not i.is_group]
        if items:
            closed = [i for i in items if i.state in closed_states(spec)]
            return f"{len(closed)}/{len(items)}"
    if spec.record_type == "task":
        kids = [c["target"] for c in db.children_of(row["id"])]
        if kids:
            term = [k for k in kids
                    if (r := _record_by_id(db, k)) is not None
                    and r["status"] in spec.status_terminal]
            return f"{len(term)}/{len(kids)}"
    return ""


def entry_lines(row: dict, spec: RecordTypeSpec, cfg: ProjConfig,
                db) -> list[str]:
    """One WIP entry (plans §5.4): `## ID. title` + entry marker + metadata
    line + summary + the checklist (its item section, groups as `###` lines)."""
    from proj.engine import items as items_mod  # lazy
    from proj.engine.store import slugify  # lazy: wip↔store cycle

    stem = f"{row['id']}-{slugify(row['title'])}"
    rel = f"{spec.dir}/{stem}.md".replace(os.sep, "/")  # path from the project root
    meta = f"> {spec.emojify(row['status'])} {row['status']} · {spec.record_type} · [{stem}]({rel})"
    prog = _progress(row, spec, db)
    if prog:
        meta += f" · {prog}"
    lines = [f"## {row['id']}. {row['title']}", ENTRY_MARK.format(id=row["id"]), meta, ""]
    if row.get("summary"):
        lines += [f"_{row['summary'].strip()}_", ""]
    if spec.items is not None:
        state = items_mod.load(db, row["id"], spec)
        state.intro = items_mod._intro_of(spec, row)
        sec = items_mod.render_section(state, spec)
        if sec.strip():
            lines += sec.splitlines()
    return lines


def render_wip_md(cfg: ProjConfig, db, registry: dict[str, RecordTypeSpec]) -> View:
    """The global WIP.md (plans §5.4): one section per registered entry, in list
    order (pos). Always created; shows `_nothing in progress_` when empty. A
    marker for a record that is no longer in the DB is dropped (it re-renders
    on the next write) — removal belongs to `proj wip return`."""
    lines = [WIP_HEADER, "# WIP", ""]
    body: list[str] = []
    for entry in db.wip_all():
        row = _record_by_id(db, entry["record_id"])
        if row is None:
            continue
        body += entry_lines(row, registry[row["type"]], cfg, db)
        body.append("")
    lines += body or ["_nothing in progress_"]
    lines += ["", FOOTER]
    return View(cfg.root / WIP_FILE, "\n".join(lines).rstrip("\n") + "\n")
