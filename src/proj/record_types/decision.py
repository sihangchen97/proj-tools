"""Decision record_type (ADR): supersede links, _TOC view, mermaid graph.

The `Decision` class holds the record_type's behavior; module-level functions are
thin delegations so call sites keep a stable shape.
"""
from __future__ import annotations

import json
from pathlib import Path

from proj.core.config import ProjConfig
from proj.engine import store as rec
from proj.engine import views
from proj.engine.record import Record, record_for, register


def _split_ids(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _append_id(v: str, new_id: str) -> str:
    ids = _split_ids(v)
    if new_id not in ids:
        ids.append(new_id)
    return ", ".join(ids)


@register
class Decision(Record):
    record_type = "decision"

    def new(self, cfg: ProjConfig, title: str, supersede: list[str] | None = None,
            summary: str = ""):
        row, p = super().new(cfg, title, summary=summary)
        supersede = supersede or []
        if not supersede:
            return row, p
        spec, db = rec.spec_and_db(cfg, self.record_type)
        olds: list[dict] = []
        for old_id in supersede:
            old = rec.by_id(cfg, spec, db, old_id)
            if old is None:
                db.close()
                raise ValueError(f"decision {old_id} not found")
            olds.append(old)
        # store canonical ids (user may have passed "1" for "0001")
        for old in olds:
            # one old decision can be superseded by several new ones
            db.set(spec, old["uuid"],
                   superseded_by=_append_id(old.get("superseded_by") or "", row["id"]))
            db.add_log(self.record_type, old["uuid"],
                       f"superseded by DEC-{row['id']}", "Changed")
        canonical = ", ".join(o["id"] for o in olds)
        db.set(spec, row["uuid"], supersedes=canonical)
        db.add_log(self.record_type, row["uuid"],
                   f"supersedes {', '.join('DEC-' + o['id'] for o in olds)}", "Added")
        db.close()
        rec.render_all(cfg)
        db = rec.open_db(cfg)
        row = rec.by_id(cfg, spec, db, row["id"])
        db.close()
        return row, p

    def toc(self, cfg: ProjConfig) -> str:
        lines = ["# Decisions", "", "| ID | Status | Title |", "|---|---|---|"]
        for r in self.list(cfg):
            lines.append(f"| {r['id']} | {self.spec.emojify(r['status'])} | {r['title']} |")
        lines.append("")
        lines += views.status_legend(self.spec)
        return "\n".join(lines) + "\n"

    def graph(self, cfg: ProjConfig) -> str:
        lines = ["graph LR"]
        for r in self.list(cfg):
            lines.append(f'  D{r["id"]}["{r["id"]} {r["title"][:24]}"]')
        for r in self.list(cfg):
            for old_id in _split_ids(r.get("supersedes")):
                lines.append(f"  D{old_id} --> D{r['id']}")
        return "\n".join(lines) + "\n"

    def link(self, cfg: ProjConfig, target: str, links: list[str]) -> dict:
        """Append cross-references to a decision's Consequences section."""
        spec, db = rec.spec_and_db(cfg, self.record_type)
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

    # ---- engine hooks ---------------------------------------------------------
    def check_rows(self, rows: list[dict], db, reg: dict) -> list[str]:
        """supersede integrity: dangling refs, A↔B mutual consistency, cycles."""
        by_id = {r["id"]: r for r in rows}
        problems: list[str] = []
        for r in by_id.values():
            for ref in _split_ids(r.get("supersedes")):
                if ref not in by_id:
                    problems.append(f"decision {r['id']}: supersedes {ref}, "
                                    f"which does not exist")
                    continue
                old = by_id[ref]
                if r["id"] not in _split_ids(old.get("superseded_by")):
                    problems.append(f"decision {ref}: superseded_by is missing "
                                    f"{r['id']} (forward ref says otherwise)")
        # cycles: DFS over supersedes edges (A → B means A supersedes B)
        edges = {i: [t for t in _split_ids(r.get("supersedes")) if t in by_id]
                 for i, r in by_id.items()}
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {i: WHITE for i in by_id}
        for start in by_id:
            if color[start] != WHITE:
                continue
            stack = [(start, iter(edges[start]))]
            color[start] = GRAY
            while stack:
                node, it = stack[-1]
                advanced = False
                for nxt in it:
                    if color[nxt] == GRAY:
                        problems.append(f"supersede cycle detected: {nxt} → … → {node}")
                    elif color[nxt] == WHITE:
                        color[nxt] = GRAY
                        stack.append((nxt, iter(edges[nxt])))
                        advanced = True
                        break
                if not advanced:
                    color[node] = BLACK
                    stack.pop()
        # de-dup while keeping order
        return list(dict.fromkeys(problems))


# ---- module-level delegations (stable call shape for cli/tests) --------------

def new(cfg: ProjConfig, title: str,
        supersede: list[str] | None = None, summary: str = "") -> tuple[dict, Path]:
    return record_for("decision", rec.load_registry(cfg.root)).new(
        cfg, title, supersede, summary)


def records(cfg: ProjConfig) -> list[dict]:
    return record_for("decision", rec.load_registry(cfg.root)).list(cfg)


def toc(cfg: ProjConfig) -> str:
    return record_for("decision", rec.load_registry(cfg.root)).toc(cfg)


def graph(cfg: ProjConfig) -> str:
    return record_for("decision", rec.load_registry(cfg.root)).graph(cfg)


def link(cfg: ProjConfig, target: str, links: list[str]) -> dict:
    return record_for("decision", rec.load_registry(cfg.root)).link(cfg, target, links)


def check(cfg: ProjConfig) -> list[str]:
    reg = rec.load_registry(cfg.root)
    db = rec.open_db(cfg)
    try:
        rows = db.all(reg["decision"])
        return record_for("decision", reg).check_rows(rows, db, reg)
    finally:
        db.close()
