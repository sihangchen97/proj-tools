"""Task record_type: parent edges (any depth), re-open restriction, `task move`."""
from __future__ import annotations

from proj.core.config import ProjConfig
from proj.engine import store as rec
from proj.engine.record import Record, register


def _ancestor_gate_violation(cfg: ProjConfig, db, record_id: str,
                             self_id: str = "") -> str | None:
    """Error text when any ANCESTOR of record_id sits in a status that is one of
    that ancestor's `gates.tasks` — i.e. the ancestor is already closed and its
    task tree must not grow again (the re-open restriction).
    self_id: the task being opened/moved (never counted as its own ancestor)."""
    # walk the parent chain from the record upward (cycle-safe via the seen set)
    cur: str | None = record_id
    seen: set[str] = {self_id}
    while cur is not None and cur not in seen:
        parent = db.parent_of(cur)
        cur = parent["source"] if parent else None
        if cur is None or cur in seen:
            break
        seen.add(cur)
        a_spec, a_row = rec.ref_record(cfg, db, cur)
        if a_row is None:
            continue  # dangling parent: flagged by `proj check`
        if a_row["status"] in a_spec.gates_tasks:
            return (f"ancestor {cur} is {a_row['status']!r} (a closed state) — "
                    f"move it out of that state first, then attach the task")
    return None


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
            # R3: the parent edge + its log line land in ONE transaction
            with rec.open_db(cfg) as db:
                db.begin()  # R3: one write transaction for the whole command
                # re-open restriction: never parent a fresh task under a closed
                # ancestor (its state is in that ancestor's gates.tasks)
                p_spec, p_row = rec.ref_record(cfg, db, canonical)
                if p_row is not None and p_row["status"] in p_spec.gates_tasks:
                    raise ValueError(
                        f"parent {canonical} is {p_row['status']!r} (a closed state) "
                        f"— move it out of that state first, or attach elsewhere")
                # kind=parent: source=parent, target=this task (single parent);
                # the edge's FK requires both ids to exist (D8)
                db.add_relation(canonical, row["id"], "parent")
                db.add_log(row["id"], f"parent: {canonical}", "Added")
            rec.render_all(cfg)
        if related:
            row = self.add_related(cfg, row["id"], related)
        return row, p

    # done/fail go through the base-class verbs (task.yaml declares them); the
    # descendant-task gate lives in Record.validate_transition (gates.tasks)

    # ---- re-open restriction -------------------------------------------------
    def validate_transition(self, cfg: ProjConfig, row: dict, verb: str, db) -> None:
        super().validate_transition(cfg, row, verb, db)
        to_s = self.verbs[verb][1]
        # opening a finished task (terminal → non-terminal) while an ancestor is
        # closed: refuse, with a hint to un-close the ancestor first
        if row["status"] in self.spec.status_terminal and to_s not in self.spec.status_terminal:
            blocked = _ancestor_gate_violation(cfg, db, row["id"])
            if blocked:
                raise ValueError(f"{self.record_type} {row['id']}: {verb} refused — {blocked}")

    def move(self, cfg: ProjConfig, id_: str, parent: str | None) -> dict:
        """Re-home an existing task (parent=None = detach to Unparented).
        Guards: target exists (any type), is not the task itself, is not one of
        its own descendants (cycle), and is not in a closed (gates.tasks) state."""
        rec.ensure_synced(cfg)  # A1
        # R3: the whole re-home (guards read, old edge dropped, new edge + log
        # written, fresh row fetched) is ONE transaction
        with rec.open_db(cfg) as db:
            db.begin()  # R3: one write transaction for the whole command
            row = rec.by_id(cfg, self.spec, db, id_)
            if row is None:
                raise ValueError(f"{self.record_type} {id_} not found")
            if parent is not None:
                self._check_parent_ref(cfg, parent)
                canonical = rec.canonical_id(cfg, parent)
                if canonical == row["id"]:
                    raise ValueError(f"cannot move {row['id']} under itself")
                if canonical in db.descendants_of(row["id"]):
                    raise ValueError(
                        f"cannot move {row['id']} under its own descendant {canonical} "
                        "(cycle)")
                p_spec, p_row = rec.ref_record(cfg, db, canonical)
                if p_row is not None and p_row["status"] in p_spec.gates_tasks:
                    raise ValueError(
                        f"parent {canonical} is {p_row['status']!r} (a closed state) "
                        f"— move it out of that state first, or attach elsewhere")
            old = db.parent_of(row["id"])
            db.set_parent(row["id"], rec.canonical_id(cfg, parent) if parent else None)
            old_s = old["source"] if old else None
            new_s = rec.canonical_id(cfg, parent) if parent else None
            db.add_log(row["id"], f"parent: {old_s} → {new_s or 'none'}", "Changed")
            fresh = rec.by_id(cfg, self.spec, db, row["id"])
        rec.render_all(cfg)
        return fresh

    # ---- engine hooks ---------------------------------------------------------
    def check_rows(self, rows: list[dict], db, reg: dict) -> list[str]:
        problems: list[str] = []
        all_ids: dict[str, str] = {}  # id -> record_type, every type
        for record_type in reg:
            for r in db.all(reg[record_type]):
                all_ids[r["id"]] = record_type
        for r in rows:
            # related refs are checked by the base check_rows (relations table)
            parent = db.parent_of(r["id"])
            if parent and parent["source"] not in all_ids:
                problems.append(f"task {r['id']}: parent {parent['source']} does not exist "
                                f"— create it or clear the task's parent")
        # parent cycles: follow the parent chain, no task may reach itself
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
        with rec.open_db(cfg) as db:
            for record_type in reg:
                if rec.by_id(cfg, reg[record_type], db, parent) is not None:
                    return
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


def move(cfg: ProjConfig, id_: str, parent: str | None) -> dict:
    return _obj(cfg).move(cfg, id_, parent)


def done(cfg: ProjConfig, id_: str) -> dict:
    return _obj(cfg).transition(cfg, "done", id_)


def fail(cfg: ProjConfig, id_: str) -> dict:
    return _obj(cfg).transition(cfg, "fail", id_)


def list_tasks(cfg: ProjConfig, status: str | None = None) -> list[dict]:
    return _obj(cfg).list(cfg, status)


def show(cfg: ProjConfig, id_: str) -> dict:
    return _obj(cfg).show(cfg, id_)
