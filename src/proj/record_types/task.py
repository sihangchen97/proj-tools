"""Task record_type: checkbox auto-promotion, done gate, plan parking."""
from __future__ import annotations

import json

from proj.core.config import ProjConfig
from proj.engine import store as rec
from proj.engine.record import Record, register
from proj.engine.views import _norm_id


@register
class Task(Record):
    record_type = "task"

    def new(self, cfg: ProjConfig, title: str, parent: str | None = None,
            summary: str = ""):
        if parent:
            self._check_parent_ref(cfg, parent)
        fields = {"parent": parent} if parent else None
        return super().new(cfg, title, fields=fields, summary=summary)

    def done(self, cfg: ProjConfig, id_: str) -> dict:
        """In Progress → Done. Requires every step checked (sub-tasks are independent)."""
        spec, db = rec.spec_and_db(cfg, self.record_type)
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
        return rec.status_set(cfg, self.record_type, id_, "Done")

    # ---- engine hooks ---------------------------------------------------------
    def on_ingest(self, db, row: dict, sections: dict, notes: list[str],
                  source_name: str) -> None:
        # first checkbox tick auto-promotes Draft → In Progress
        if row["status"] == "Draft" and rec.has_checked(sections.get("steps", "")):
            db.set(self.spec, row["uuid"], status="In Progress")
            db.add_log(self.record_type, row["uuid"], "status Draft → In Progress", "Changed")
            notes.append(f"{source_name}: auto status Draft → In Progress")

    def check_rows(self, rows: list[dict], db, reg: dict) -> list[str]:
        problems: list[str] = []
        all_ids: dict[str, str] = {}  # norm(id) -> record_type, every type
        for record_type in reg:
            for r in db.all(reg[record_type]):
                all_ids[_norm_id(r["id"])] = record_type
        for r in rows:
            steps = json.loads(r["sections"] or "{}").get("steps", "")
            if any(i >= 2 for i, _, _ in rec.checkboxes(steps)):
                problems.append(f"task {r['id']}: Steps deeper than 2 levels "
                                f"— flatten sub-tasks")
            parent = (r.get("parent") or "").strip()
            if parent and _norm_id(parent) not in all_ids:
                problems.append(f"task {r['id']}: parent {parent} does not exist "
                                f"— create it or clear the task's parent field")
            for ref in (r.get("related") or "").replace(",", " ").split():
                if _norm_id(ref) not in all_ids:
                    problems.append(f"task {r['id']}: related {ref} does not exist")
        # parent cycles: follow the parent chain, no task may reach itself
        by_norm = {_norm_id(r["id"]): r for r in rows}
        for r in rows:
            seen, cur = set(), by_norm.get(_norm_id(r["id"]))
            while cur is not None and (cur.get("parent") or "").strip():
                parent = _norm_id(cur["parent"].strip())
                if parent in seen:
                    problems.append(f"task {r['id']}: parent cycle "
                                    f"({r['id']} → {parent} → …)")
                    break
                seen.add(parent)
                cur = by_norm.get(parent)
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


def new(cfg: ProjConfig, title: str, parent: str | None = None, summary: str = ""):
    return _obj(cfg).new(cfg, title, parent, summary)


def done(cfg: ProjConfig, id_: str) -> dict:
    return _obj(cfg).done(cfg, id_)


def list_tasks(cfg: ProjConfig, status: str | None = None) -> list[dict]:
    return _obj(cfg).list(cfg, status)


def show(cfg: ProjConfig, id_: str) -> dict:
    return _obj(cfg).show(cfg, id_)
