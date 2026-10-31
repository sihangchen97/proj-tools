"""Task record_type: checkbox auto-promotion, done gate, plan parking."""
from __future__ import annotations

import json

from proj.core.config import ProjConfig
from proj.engine import store as rec
from proj.engine.record import Record, register


@register
class Task(Record):
    record_type = "task"

    def new(self, cfg: ProjConfig, title: str, parent: str | None = None,
            summary: str = "", commit: str = "",
            related: list[str] | None = None):
        row, p = super().new(cfg, title, summary=summary, commit=commit)
        if parent:
            self._check_parent_ref(cfg, parent)
            canonical = rec.canonical_id(cfg, parent)
            db = rec.open_db(cfg)
            try:
                # kind=parent: source=parent, target=this task (single parent);
                # the edge's FK requires both ids to exist (D8)
                db.add_relation(canonical, row["id"], "parent")
                db.add_log(row["id"], f"parent: {canonical}", "Added")
            finally:
                db.close()
            rec.render_all(cfg)
        if related:
            row = self.add_related(cfg, row["id"], related)
        return row, p

    # done goes through the base-class verb (task.yaml declares `done`); the
    # checkbox gate lives in Record.validate_transition

    # ---- engine hooks ---------------------------------------------------------
    def on_ingest(self, db, row: dict, sections: dict, notes: list[str],
                   source_name: str) -> None:
        # first checkbox tick auto-promotes Draft → In Progress
        if row["status"] == "Draft" and rec.has_checked(sections.get("steps", "")):
            db.set(self.spec, row["id"], status="In Progress")
            db.add_log(row["id"], category="Changed",
                       from_status="Draft", to_status="In Progress")
            notes.append(f"{source_name}: auto status Draft → In Progress")

    def check_rows(self, rows: list[dict], db, reg: dict) -> list[str]:
        problems: list[str] = []
        all_ids: dict[str, str] = {}  # id -> record_type, every type
        for record_type in reg:
            for r in db.all(reg[record_type]):
                all_ids[r["id"]] = record_type
        for r in rows:
            steps = json.loads(r["sections"] or "{}").get("steps", "")
            if any(i >= 2 for i, _, _ in rec.checkboxes(steps)):
                problems.append(f"task {r['id']}: Steps deeper than 2 levels "
                                f"— flatten sub-tasks")
            # related refs are checked by the base check_rows (relations table)
            parent = db.parent_of(r["id"])
            if parent and parent["source"] not in all_ids:
                problems.append(f"task {r['id']}: parent {parent['source']} does not exist "
                                f"— create it or clear the task's parent")
        # parent cycles: follow the parent chain, no task may reach itself
        # (only follow parents that are tasks; a plan/experiment root ends the chain)
        by_id = {r["id"]: r for r in rows}
        for r in rows:
            seen, cur = set(), by_id.get(r["id"])
            while cur is not None:
                parent = db.parent_of(cur["id"])
                if parent is None:
                    break
                if parent["source"] in seen:
                    problems.append(f"task {r['id']}: parent cycle "
                                    f"({r['id']} → {parent['source']} → …)")
                    break
                seen.add(parent["source"])
                cur = by_id.get(parent["source"])
        return problems

    # ---- helpers ---------------------------------------------------------------
    def _check_parent_ref(self, cfg: ProjConfig, parent: str) -> None:
        """A task pointing at a nonexistent parent would render nowhere, silently."""
        reg = rec.load_registry(cfg.root)
        db = rec.open_db(cfg)
        try:
            for record_type in reg:
                if rec.by_id(cfg, reg[record_type], db, parent) is not None:
                    return
        finally:
            db.close()
        raise ValueError(
            f"parent {parent!r} not found in any record_type "
            f"({', '.join(reg)}) — create it first or drop --parent")



# ---- module-level delegations (stable call shape for cli/tests) --------------

def _obj(cfg: ProjConfig) -> Task:
    return Task(rec.load_registry(cfg.root)["task"])


def new(cfg: ProjConfig, title: str, parent: str | None = None,
        summary: str = "", commit: str = "",
        related: list[str] | None = None):
    return _obj(cfg).new(cfg, title, parent, summary, commit, related)


def done(cfg: ProjConfig, id_: str) -> dict:
    return _obj(cfg).transition(cfg, "done", id_)


def list_tasks(cfg: ProjConfig, status: str | None = None) -> list[dict]:
    return _obj(cfg).list(cfg, status)


def show(cfg: ProjConfig, id_: str) -> dict:
    return _obj(cfg).show(cfg, id_)
