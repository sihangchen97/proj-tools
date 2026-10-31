"""Plan record_type: the review gate (all direct child tasks must be Done)."""
from __future__ import annotations

from proj.core.config import ProjConfig
from proj.engine import store as rec
from proj.engine.record import Record, register


@register
class Plan(Record):
    record_type = "plan"

    def validate_transition(self, cfg: ProjConfig, row: dict, verb: str, db) -> None:
        super().validate_transition(cfg, row, verb, db)
        to_s = self.verbs[verb][1]
        if to_s != "Pending Review":
            return
        task_spec = rec.load_registry(cfg.root)["task"]
        kids = [c["target"] for c in db.children_of(row["id"])]
        todo = [r["id"] for r in db.all(task_spec)
                if r["id"] in kids and r["status"] != "Done"]
        if todo:
            raise ValueError(
                f"plan {row['id']}: {len(todo)} direct task(s) not Done block {verb}: "
                + ", ".join(todo))
