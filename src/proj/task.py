"""`proj task` — thin wrapper over the engine (task kind)."""
from __future__ import annotations

import json

from proj import records as rec
from proj.config import ProjConfig


def _check_plan_ref(cfg: ProjConfig, plan: str) -> None:
    """A task pointing at a nonexistent plan would park forever, silently."""
    spec = rec.load_registry(cfg.root)["plan"]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, plan)
    db.close()
    if row is None:
        raise ValueError(f"plan {plan} not found — create it first "
                         f"(`proj plan new ...`) or drop --plan")


def new(cfg: ProjConfig, title: str, plan: str | None = None):
    if plan:
        _check_plan_ref(cfg, plan)
    fields = {"plan": plan or ""} if plan else None
    return rec.new_record(cfg, "task", title, fields=fields)


def done(cfg: ProjConfig, id_: str):
    """In progress → Done. Requires every checkbox (both levels) checked."""
    spec = rec.load_registry(cfg.root)["task"]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        raise ValueError(f"task {id_} not found")
    if row["status"] == "Done":
        db.close()
        raise ValueError(f"task {id_} is already Done")
    sections = json.loads(row["sections"] or "{}")
    unchecked = [t for _, c, t in rec.checkboxes(sections.get("steps", "")) if not c]
    db.close()
    if unchecked:
        raise ValueError(f"task {id_} has {len(unchecked)} unchecked step(s):\n  "
                         + "\n  ".join(unchecked))
    return rec.status_set(cfg, "task", id_, "Done")


def list_tasks(cfg: ProjConfig, status: str | None = None) -> list[dict]:
    spec = rec.load_registry(cfg.root)["task"]
    db = rec.open_db(cfg)
    rows = db.all(spec)
    db.close()
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows


def show(cfg: ProjConfig, id_: str) -> dict:
    spec = rec.load_registry(cfg.root)["task"]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, id_)
    logs = db.logs("task", row["uuid"]) if row else []
    db.close()
    if row is None:
        raise ValueError(f"task {id_} not found")
    return row | {"logs": logs}
