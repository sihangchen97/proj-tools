"""Record behavior layer: a *template* drives a *record* (the Record class
hierarchy). Storage mechanics (md parse/render, DB ops, sync/adopt) live in
store.py — this module imports it at top level, store.py calls back lazily.

Flow (matches how the engine already works):

    load_registry()            -> {record_type: RecordTypeSpec}   # read every YAML template
    record_for(record_type, registry) -> Record subclass bound to that RecordTypeSpec

A `Record` is constructed *from* its template (the `RecordTypeSpec`), so the
template→record derivation is explicit: the class carries the schema it
behaves on. Kind-specific behavior (state machines, supersede, task parking)
lives in subclasses under proj.record_types; a record_type with only a YAML profile gets the
vanilla `Record`. New record_type = one YAML file, still — a class is optional.
"""
from __future__ import annotations

from proj.core.config import ProjConfig
from proj.core.templates import RecordTypeSpec
from proj.engine import store as rec


def _split_ids(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _append_id(v: str, new_id: str) -> str:
    ids = _split_ids(v)
    if new_id not in ids:
        ids.append(new_id)
    return ", ".join(ids)


class Record:
    """Base record_type: everything the generic engine already does for any template."""

    record_type = ""

    def __init__(self, spec: RecordTypeSpec):
        self.spec = spec  # the template this record was derived from
        self.record_type = spec.record_type  # instance-level; subclasses also set the class attr
        self.verbs = dict(spec.verbs)  # state machine verbs come from the template (yaml)
        # every verb is also registered as a same-name method on the instance:
        # record.activate(cfg, id) == record.transition(cfg, "activate", id)
        for verb in self.verbs:
            setattr(self, verb, (lambda cfg, id_, _v=verb: self.transition(cfg, _v, id_)))

    # ---- generic operations (operate on self.spec) ---------------------------
    def new(self, cfg: ProjConfig, title: str, summary: str = "",
            commit: str = "", supersede: list[str] | None = None,
            related: list[str] | None = None):
        row, p = rec.new_record(cfg, self.spec, title, summary=summary, commit=commit)
        supersede = supersede or []
        related = related or []
        if not supersede and not related:
            return row, p
        # R3: every edge + its changelog lines land in ONE transaction
        with rec.open_db(cfg) as db:
            db.begin()  # R3: one write transaction for the whole command
            for old_id in supersede:
                # supersede is a pure relation on every record_type (D1): the
                # target must exist and be the SAME type; no status side effect
                spec, old = rec.ref_record(cfg, db, old_id)
                if old is None:
                    raise ValueError(f"supersede {old_id!r} does not match any record "
                                     f"\u2014 use the canonical id (e.g. DEC-0001)")
                if spec.record_type != self.record_type:
                    raise ValueError(f"supersede {old_id} is a {spec.record_type}, "
                                     f"not a {self.record_type} — same type required")
                if old["id"] == row["id"]:
                    raise ValueError(f"{self.record_type} cannot supersede itself")
                db.add_relation(row["id"], old["id"], "supersede")
                db.add_log(old["id"], f"superseded by {row['id']}", "Changed")
            if supersede:
                db.add_log(row["id"], "supersedes " + ", ".join(supersede), "Added")
            for ref in related:
                _spec, target = rec.ref_record(cfg, db, ref)
                if target is None:
                    raise ValueError(f"related {ref!r} does not match any record "
                                     f"\u2014 use the canonical id (e.g. PLN-0001)")
                if target["id"] == row["id"]:
                    raise ValueError(f"cannot relate {self.record_type} {row['id']} to itself")
                db.add_relation(row["id"], target["id"], "related")
            if related:
                db.add_log(row["id"], "related: " + ", ".join(related), "Added")
        rec.render_all(cfg)
        # the final fresh row is a plain read — one short-lived connection
        with rec.open_db(cfg) as db:
            fresh = rec.by_id(cfg, rec.spec_of(cfg, self.record_type), db, row["id"])
        return fresh, p

    def add_related(self, cfg: ProjConfig, id_: str, refs: list[str]) -> dict:
        """Attach related records (any type, unordered) to an existing record."""
        rec.ensure_synced(cfg)  # A1
        # R3: the related edges + changelog line land in ONE transaction
        with rec.open_db(cfg) as db:
            db.begin()  # R3: one write transaction for the whole command
            row = rec.by_id(cfg, self.spec, db, id_)
            if row is None:
                raise ValueError(f"{self.record_type} {id_} not found")
            for ref in refs:
                _spec, target = rec.ref_record(cfg, db, ref)
                if target is None:
                    raise ValueError(f"related {ref!r} does not match any record "
                                     "\u2014 use the canonical id (e.g. PLN-0001)")
                if target["id"] == row["id"]:
                    raise ValueError(f"cannot relate {self.record_type} {row['id']} to itself")
                db.add_relation(row["id"], target["id"], "related")
            db.add_log(row["id"], "related: " + ", ".join(refs), "Added")
        rec.render_all(cfg)
        with rec.open_db(cfg) as db:
            fresh = rec.by_id(cfg, self.spec, db, id_)
        return fresh

    def list(self, cfg: ProjConfig, status: str | None = None) -> list[dict]:
        with rec.open_db(cfg) as db:
            rows = db.all(self.spec)
        if status:
            rows = [r for r in rows if r["status"] == status]
        return rows

    def show(self, cfg: ProjConfig, id_: str) -> dict:
        with rec.open_db(cfg) as db:
            row = rec.by_id(cfg, self.spec, db, id_)
            logs = db.logs(row["id"]) if row else []
        if row is None:
            raise ValueError(f"{self.record_type} {id_} not found")
        return row | {"logs": logs}

    def set_status(self, cfg: ProjConfig, id_: str, status: str,
                   when: str | None = None) -> dict:
        """Set this record_type's status (validated against the template's allowed set)."""
        return rec.status_set(cfg, self.record_type, id_, status, when)

    def add_log(self, cfg: ProjConfig, id_: str, summary: str,
                category: str | None = None) -> None:
        """Append a changelog entry to this record: 'Category: text' (or plain text)."""
        rec.add_log(cfg, self.record_type, id_, summary, category)

    def transition(self, cfg: ProjConfig, verb: str, id_: str) -> dict:
        """State-machine step: verb maps to (from_status, to_status)."""
        if verb not in self.verbs:
            raise ValueError(f"unknown verb {verb!r} (expected: {', '.join(self.verbs)})")
        # R3: the gate check is read-only (its own connection); the status change
        # is the single write transaction (status_set)
        with rec.open_db(cfg) as db:
            row = rec.by_id(cfg, self.spec, db, id_)
            if row is None:
                raise ValueError(f"{self.record_type} {id_} not found")
            (from_s, to_s) = self.verbs[verb]
            allowed = from_s if isinstance(from_s, list) else [from_s]
            if from_s != "Any" and row["status"] not in allowed:
                raise ValueError(f"{self.record_type} {id_} is {row['status']!r}, "
                                 f"{verb} requires {from_s!r}")
            self.validate_transition(cfg, row, verb, db)
        return rec.status_set(cfg, self.record_type, id_, to_s)

    # ---- extension points (subclasses override) ------------------------------
    def validate_transition(self, cfg: ProjConfig, row: dict, verb: str, db) -> None:
        """Template-declared gates (profile `gates:`): entering a `gates.tasks`
        status requires every DESCENDANT task (any depth) to sit in a terminal
        task status (task's `status_terminal`: Done, Failed, ...); entering a
        `gates.items` status requires every item of this record to be closed."""
        to_s = self.verbs[verb][1]
        if self.spec.items is not None and to_s in self.spec.gates_items:
            from proj.engine import items as items_mod  # lazy: record↔items cycle
            state = items_mod.load(db, row["id"], self.spec)
            state.intro = items_mod._intro_of(self.spec, row)
            un = items_mod.open_items(state, self.spec)
            if un:
                raise ValueError(
                    f"{self.record_type} {row['id']}: {len(un)} unclosed {self.spec.items.noun}(s) "
                    f"block {verb} (need a closed state: "
                    f"{', '.join(items_mod.CLOSED_STATES)}):\n  "
                    + "\n  ".join(un))
        if to_s not in self.spec.gates_tasks:
            return
        reg = rec.load_registry(cfg.root)
        task_spec = reg.get("task")
        if task_spec is None:
            return
        desc = db.descendants_of(row["id"])
        bad = [f"{r['id']} ({r['status']})" for r in db.all(task_spec)
               if r["id"] in desc and r["status"] not in task_spec.status_terminal]
        if bad:
            raise ValueError(
                f"{self.record_type} {row['id']}: {len(bad)} task(s) not finished block "
                f"{verb} (need a terminal task status: "
                f"{', '.join(task_spec.status_terminal)}):\n  " + "\n  ".join(bad))

    def on_ingest(self, db, row: dict, sections: dict, notes: list[str],
                  source_name: str) -> None:
        """Hook after md ingest (sync/adopt). Base: no-op."""

    def check_rows(self, rows: list[dict], db, reg: dict) -> list[str]:
        """`proj check` invariants: ref integrity + cycle check over the relations
        table (supersede edges are same-type on every record_type; related may
        point at any type)."""
        all_ids: dict[str, str] = {r["id"]: rt for rt in reg for r in db.all(reg[rt])}
        problems = []
        for r in rows:
            for edge in db.supersede_of(r["id"]):
                if edge["target"] not in all_ids:
                    problems.append(
                        f"{self.record_type} {r['id']}: supersedes {edge['target']}, "
                        f"which does not exist")
            for ref in db.related_of(r["id"]):
                if ref not in all_ids:
                    problems.append(
                        f"{self.record_type} {r['id']}: related {ref}, which does not exist")
        by_id = {r["id"]: r for r in rows}
        edges = {
            i: [e["target"] for e in db.supersede_of(i) if e["target"] in by_id]
            for i in by_id
        }
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
        return list(dict.fromkeys(problems))


_kind_classes: dict[str, type[Record]] = {}


def register(cls: type[Record]) -> type[Record]:
    _kind_classes[cls.record_type] = cls
    return cls


def record_class(record_type: str) -> type[Record]:
    """The Record subclass for a record_type; pure-YAML record_types fall back to base Record."""
    if not _kind_classes:
        import proj.record_types  # noqa: F401 — package import triggers registration
    return _kind_classes.get(record_type, Record)


def record_for(record_type, registry: dict[str, RecordTypeSpec] | None = None) -> Record:
    """Derive a Record instance from its template: class(record_type) bound to the RecordTypeSpec.

    Accepts a record_type name (needs `registry`) or a RecordTypeSpec directly."""
    if isinstance(record_type, RecordTypeSpec):
        spec = record_type
        record_type = spec.record_type
    elif registry is not None:
        spec = registry[record_type]
    else:
        spec = rec.load_registry()[record_type]
    return record_class(record_type)(spec)
