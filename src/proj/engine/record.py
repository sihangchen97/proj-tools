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
    def new(self, cfg: ProjConfig, title: str, fields: dict | None = None,
            summary: str = ""):
        return rec.new_record(cfg, self.spec, title, fields=fields, summary=summary)

    def list(self, cfg: ProjConfig, status: str | None = None) -> list[dict]:
        spec, db = rec.spec_and_db(cfg, self.record_type)
        rows = db.all(spec)
        db.close()
        if status:
            rows = [r for r in rows if r["status"] == status]
        return rows

    def show(self, cfg: ProjConfig, id_: str) -> dict:
        spec, db = rec.spec_and_db(cfg, self.record_type)
        row = rec.by_id(cfg, spec, db, id_)
        logs = db.logs(self.record_type, row["uuid"]) if row else []
        db.close()
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
        spec, db = rec.spec_and_db(cfg, self.record_type)
        row = rec.by_id(cfg, spec, db, id_)
        if row is None:
            db.close()
            raise ValueError(f"{self.record_type} {id_} not found")
        (from_s, to_s) = self.verbs[verb]
        if row["status"] != from_s:
            db.close()
            raise ValueError(f"{self.record_type} {id_} is {row['status']!r}, "
                             f"{verb} requires {from_s!r}")
        try:
            self.validate_transition(cfg, row, verb, db)
        finally:
            db.close()
        return rec.status_set(cfg, self.record_type, id_, to_s)

    # ---- extension points (subclasses override) ------------------------------
    def validate_transition(self, cfg: ProjConfig, row: dict, verb: str, db) -> None:
        """Kind-specific precondition for a status transition."""

    def on_ingest(self, db, row: dict, sections: dict, notes: list[str],
                  source_name: str) -> None:
        """Hook after md ingest (sync/adopt). Base: no-op."""

    def check_rows(self, rows: list[dict], db, reg: dict) -> list[str]:
        """`proj check` invariants for this record_type's rows."""
        return []


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
