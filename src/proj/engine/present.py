"""D5: single formatting for record output — the CLI and the MCP server both
render from here, so a `show` can no longer look different in one than the other.

`show(spec, row, db, history)` returns ``(text, data)``: the human-readable
lines and the JSON object (the same fields, machine-readable). `list_lines(rows)`
is the shared one-line-per-record text used by `ls`."""
from __future__ import annotations

from ..core.db import show_date
from . import items as items_mod
from . import store as rec


def _parent_of(db, row: dict) -> str | None:
    p = db.parent_of(row["id"])
    return p["source"] if p else None


def show(spec, row: dict, db, history: bool = False) -> tuple[str, dict]:
    """One record, formatted once (D5): human lines + a JSON dict (same fields)."""
    parent = _parent_of(db, row)
    items_text = ""
    if spec.primary_items is not None:
        state = items_mod.load(db, row["id"], spec)
        state.intro = items_mod._intro_of(spec, row)
        items_text = items_mod.render_section(state, spec) or ""
    logs = db.logs(row["id"])

    data = {
        "id": row["id"],
        "type": spec.record_type,
        "title": row["title"],
        "status": row["status"],
        "created": show_date(row["created"]),
        "summary": row.get("summary") or None,
        "commit": row.get("commit") or None,
        "parent": parent,
        # K4: the records whose checklist items reference this task (task only)
        "picked_by": (db.picked_by(row["id"])
                      if spec.record_type == "task" else None),
        "changelog": [{"date": show_date(e["date"]), "category": e["category"],
                       "text": rec._log_body(e, spec),
                       "actor": e.get("actor") or None} for e in logs],
    }

    lines = [f"id: {row['id']}", f"title: {row['title']}",
             f"status: {row['status']}", f"created: {data['created']}"]
    if data["summary"]:
        lines.append(f"summary: {data['summary']}")
    if data["commit"]:
        lines.append(f"commit: {data['commit']}")
    if parent:
        lines.append(f"parent: {parent}")
    if items_text:
        lines += ["", f"{spec.primary_items.section}:", items_text]
    if history:
        for e in logs:
            line = f"  {show_date(e['date'])}  [{e['category']}] {rec._log_body(e, spec)}"
            if e.get("actor"):
                line += f" · {e['actor']}"
            lines.append(line)
    return "\n".join(lines), data


def list_lines(rows: list[dict], status: str | None = None) -> str:
    """One line per record: id, status, title (shared by the CLI `ls` and MCP `proj_ls`)."""
    if not rows:
        return "(no records)" + (f" with status {status!r}" if status else "")
    return "\n".join(f"{r['id']:<8} {r['status']:<24} {r['title']}" for r in rows)
