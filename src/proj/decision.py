"""`proj decision` — the v2 frontmatter engine, decision kind."""
from __future__ import annotations

import re
from datetime import date

from proj import index as idx
from proj.config import ProjConfig
from proj.templates import load_registry

SEEN_RE = re.compile(r"^[-*]\s*See also:\s*(.+)$", re.MULTILINE)


def _spec(cfg: ProjConfig):
    return load_registry(cfg.root)["decision"]


def new(cfg: ProjConfig, title: str, supersede: str | None = None) -> idx.FMRecord:
    spec = _spec(cfg)
    rec = idx.new_record(cfg, spec, title)
    if supersede:
        old = idx.by_id(cfg, "decision", supersede)
        if old is None:
            raise ValueError(f"decision {supersede} not found in {cfg.root / spec.dir}")
        old.status_set(f"Superseded by DEC-{rec.id}")
        old.write()
        idx.upsert(cfg, old)
        rec.meta["supersedes"] = old.id
        rec.status_set(f"Supersedes DEC-{old.id}")
        rec.write()
    idx.upsert(cfg, rec)
    return rec


def _records(cfg: ProjConfig) -> list[idx.FMRecord]:
    d = cfg.root / _spec(cfg).dir
    if not d.is_dir():
        return []
    return [r for r in idx.all_records(cfg) if r.kind == "decision"]


def status_set_all(cfg: ProjConfig) -> list[idx.FMRecord]:
    """Re-read all decision records (helper for CLI)."""
    return _records(cfg)


def toc(cfg: ProjConfig) -> str:
    lines = ["# Decisions", ""]
    for rec in _records(cfg):
        lines.append(f"- {rec.display}")
    return "\n".join(lines) + "\n"


def graph(cfg: ProjConfig) -> str:
    recs = _records(cfg)
    lines = ["graph LR"]
    edges = []
    for rec in recs:
        label = f"D{rec.id}[\"{rec.id} {rec.title[:24]}\"]"
        lines.append(f"  {label}")
        sup = rec.meta.get("supersedes")
        if sup:
            edges.append(f"  D{sup} --> D{rec.id}")
    lines.extend(edges)
    return "\n".join(lines) + "\n"


def link(cfg: ProjConfig, target: str, links: list[str]) -> idx.FMRecord:
    """Append cross-references to a decision's 'See also' list."""
    rec = idx.by_id(cfg, "decision", target)
    if rec is None:
        raise ValueError(f"decision {target} not found")
    items = [f"DEC-{n}" for n in links]
    m = SEEN_RE.search(rec.body)
    if m:
        existing = [i.strip() for i in m.group(1).split(",")]
        items = existing + [i for i in items if i not in existing]
        rec.body = rec.body[: m.start()] + "See also: " + ", ".join(items) + rec.body[m.end():]
    else:
        rec.body = rec.body.rstrip() + "\n\nSee also: " + ", ".join(items) + "\n"
    rec.write()
    idx.upsert(cfg, rec)
    return rec


def set_status(cfg: ProjConfig, id_: str, status: str) -> idx.FMRecord:
    rec = idx.by_id(cfg, "decision", id_)
    if rec is None:
        raise ValueError(f"decision {id_} not found")
    allowed = _spec(cfg).status_allowed
    if not any(re.fullmatch(a, status) for a in allowed):
        raise ValueError(f"status {status!r} not allowed for decision "
                         f"(allowed: {', '.join(allowed)})")
    rec.status_set(status)
    idx.upsert(cfg, rec)
    return rec


def _today() -> str:
    return date.today().isoformat()
