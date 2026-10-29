"""`proj plan` — state machine on top of the engine (plan kind).

Draft → Active → Pending review → Shelved | Archived
          ↑____________ resume ____________|
"""
from __future__ import annotations

from proj import records as rec
from proj.config import ProjConfig

TRANSITIONS = {
    "activate": ("Draft", "Active"),
    "resume": ("Shelved", "Active"),
    "review": ("Active", "Pending review"),
    "shelve": ("Pending review", "Shelved"),
    "archive": ("Pending review", "Archived"),
}


def new(cfg: ProjConfig, title: str):
    return rec.new_record(cfg, "plan", title)


def _plan_row(cfg: ProjConfig, id_: str) -> tuple[object, object, dict]:
    spec = rec.load_registry(cfg.root)["plan"]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, id_)
    return db, row


def transition(cfg: ProjConfig, verb: str, id_: str):
    db, row = _plan_row(cfg, id_)
    if row is None:
        db.close()
        raise ValueError(f"plan {id_} not found")
    if verb not in TRANSITIONS:
        db.close()
        raise ValueError(f"unknown verb {verb!r} (expected: {', '.join(TRANSITIONS)})")
    (from_s, to_s) = TRANSITIONS[verb]
    if row["status"] != from_s:
        db.close()
        raise ValueError(f"plan {id_} is {row['status']!r}, {verb} requires {from_s!r}")
    if verb == "review":
        def _norm(v: str) -> str:
            return str(int(v)) if v.isdigit() else v

        target = _norm(row["id"])
        tasks = [t for t in db.all(rec.load_registry(cfg.root)["task"])
                 if _norm((t.get("plan") or "").strip()) == target]
        pending = [t for t in tasks if t["status"] != "Done"]
        if pending:
            db.close()
            raise ValueError("plan has unfinished tasks:\n  " + "\n  ".join(
                f"{t['id']} {t['status']} — {t['title']}" for t in pending))
        if not tasks:
            db.close()
            raise ValueError("plan has no tasks — nothing to review")
    db.close()
    return rec.status_set(cfg, "plan", id_, to_s)


def list_plans(cfg: ProjConfig, status: str | None = None) -> list[dict]:
    spec = rec.load_registry(cfg.root)["plan"]
    db = rec.open_db(cfg)
    rows = db.all(spec)
    db.close()
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows
