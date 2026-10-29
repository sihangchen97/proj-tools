"""`proj decision` — ADR-specific renderers + supersede on top of the engine."""
from __future__ import annotations

from pathlib import Path

from proj import records as rec
from proj.config import ProjConfig


def new(cfg: ProjConfig, title: str, supersede: str | None = None) -> tuple[dict, Path]:
    row, p = rec.new_record(cfg, "decision", title)
    if supersede:
        spec = rec.load_registry(cfg.root)["decision"]
        db = rec.open_db(cfg)
        old = rec.by_id(cfg, spec, db, supersede)
        if old is None:
            db.close()
            raise ValueError(f"decision {supersede} not found")
        db.set(spec, old["uuid"], status=f"Superseded by DEC-{row['id']}")
        db.add_log("decision", old["uuid"],
                   f"status {old['status']} → Superseded by DEC-{row['id']}", "Changed")
        db.set(spec, row["uuid"], supersedes=old["id"],
               status=f"Supersedes DEC-{old['id']}")
        db.add_log("decision", row["uuid"],
                   f"status Proposed → Supersedes DEC-{old['id']}", "Changed")
        db.close()
        rec.render_all(cfg)
        db = rec.open_db(cfg)
        row = rec.by_id(cfg, spec, db, row["id"])
        db.close()
    return row, p


def _spec(cfg: ProjConfig):
    return rec.load_registry(cfg.root)["decision"]


def records(cfg: ProjConfig) -> list[dict]:
    spec = _spec(cfg)
    db = rec.open_db(cfg)
    rows = db.all(spec)
    db.close()
    return rows


def toc(cfg: ProjConfig) -> str:
    lines = ["# Decisions", ""]
    for r in records(cfg):
        lines.append(f"- {r['id']}  {r['status']:<22} {r['title']}")
    return "\n".join(lines) + "\n"


def graph(cfg: ProjConfig) -> str:
    rows = records(cfg)
    lines = ["graph LR"]
    for r in rows:
        lines.append(f'  D{r["id"]}["{r["id"]} {r["title"][:24]}"]')
    for r in rows:
        if r.get("supersedes"):
            lines.append(f'  D{r["supersedes"]} --> D{r["id"]}')
    return "\n".join(lines) + "\n"


def link(cfg: ProjConfig, target: str, links: list[str]) -> dict:
    """Append cross-references to a decision's Consequences section."""
    import json
    spec = _spec(cfg)
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, target)
    if row is None:
        db.close()
        raise ValueError(f"decision {target} not found")
    sections = json.loads(row["sections"] or "{}")
    cons = sections.get("consequences", "").rstrip()
    items = [f"DEC-{n}" for n in links]
    ref = "See also: " + ", ".join(
        [i for i in items if i not in cons] or [items[0]])
    sections["consequences"] = (cons + "\n\n" + ref) if cons else ref
    db.set(spec, row["uuid"], sections=json.dumps(sections, ensure_ascii=False))
    fresh = rec.by_id(cfg, spec, db, target)
    p = rec.file_path(cfg, spec, fresh)
    p.write_text(rec.render_record(spec, fresh, db), encoding="utf-8")
    db.close()
    return row
