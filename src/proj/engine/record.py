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

import json

from proj.core.config import ProjConfig
from proj.core.templates import RecordTypeSpec
from proj.engine import store as rec
from proj.engine import views


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
        db = rec.open_db(cfg)
        try:
            for i, old_id in enumerate(supersede):
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
                db.add_relation(row["id"], old["id"], "supersede", pos=i)
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
        finally:
            db.close()
        rec.render_all(cfg)
        spec, db = rec.spec_and_db(cfg, self.record_type)
        fresh = rec.by_id(cfg, spec, db, row["id"])
        db.close()
        return fresh, p

    def add_related(self, cfg: ProjConfig, id_: str, refs: list[str]) -> dict:
        """Attach related records (any type, unordered) to an existing record."""
        db = rec.open_db(cfg)
        try:
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
        finally:
            db.close()
        rec.render_all(cfg)
        spec, db = rec.spec_and_db(cfg, self.record_type)
        fresh = rec.by_id(cfg, spec, db, id_)
        db.close()
        return fresh

    def toc(self, cfg: ProjConfig) -> str:
        """ID/status/title table + status legend (render.toc record_types)."""
        title = "# " + self.record_type.title() + "s"
        header = [title, "", "| ID | Status | Title |", "|---|---|---|"]
        lines = header
        for r in self.list(cfg):
            lines.append(f"| {r['id']} | {self.spec.emojify(r['status'])} | {r['title']} |")
        lines.append("")
        lines += views.status_legend(self.spec)
        return "\n".join(lines) + "\n"

    def graph(self, cfg: ProjConfig) -> str:
        """Mermaid graph of supersedes edges (supersede is a relation on every
        record_type — the edges render when they exist)."""
        lines = ["graph LR"]
        for r in self.list(cfg):
            lines.append(f'  {r["id"]}["{r["id"]} {r["title"][:24]}"]')
        spec, db = rec.spec_and_db(cfg, self.record_type)
        for r in db.all(spec):
            for edge in db.supersede_of(r["id"]):
                lines.append(f"  {edge['target']} --> {r['id']}")
        db.close()
        return "\n".join(lines) + "\n"

    def link(self, cfg: ProjConfig, target: str, links: list[str],
             section: str = "consequences") -> dict:
        """Append cross-references to a record's `section` body (user text wins)."""
        spec, db = rec.spec_and_db(cfg, self.record_type)
        row = rec.by_id(cfg, spec, db, target)
        if row is None:
            db.close()
            raise ValueError(f"{self.record_type} {target} not found")
        sections = json.loads(row["sections"] or "{}")
        body = sections.get(section, "").rstrip()
        refs = list(links)  # ids are stored canonical ({code}{n:0w})
        ref_line = "See also: " + ", ".join([x for x in refs if x not in body] or [refs[0]])
        sections[section] = (body + "\n\n" + ref_line) if body else ref_line
        db.set(spec, row["id"], sections=json.dumps(sections, ensure_ascii=False))
        fresh = rec.by_id(cfg, spec, db, target)
        p = rec.file_path(cfg, spec, fresh)
        p.write_text(rec.render_record(spec, fresh, db), encoding="utf-8")
        db.close()
        return row

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
        logs = db.logs(row["id"]) if row else []
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
        allowed = from_s if isinstance(from_s, list) else [from_s]
        if from_s != "Any" and row["status"] not in allowed:
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
        """Generic gate: entering a done status requires every checkbox checked."""
        to_s = self.verbs[verb][1]
        if to_s not in self.spec.status_done:
            return
        sections = json.loads(row.get("sections") or "{}")
        unchecked = [txt for text in sections.values()
                     for _, c, txt in rec.checkboxes(text) if not c]
        if unchecked:
            raise ValueError(
                f"{self.record_type} {row['id']}: {len(unchecked)} unchecked step(s) "
                f"block {verb}:\n  " + "\n  ".join(unchecked))

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
