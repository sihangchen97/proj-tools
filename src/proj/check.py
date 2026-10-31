"""`proj check` — invariants across DB, lock, templates and md files."""
from __future__ import annotations

import json
import re

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.core.templates import load_registry
from proj.engine import store as rec
from proj.engine.record import record_for


def check(cfg: ProjConfig) -> list[str]:
    problems: list[str] = []
    if not cfg.db_path.is_file():
        return ["no .proj.sqlite — run `proj init`"]

    lock = dbmod.read_lock(cfg.root)
    if lock is None:
        problems.append("no .proj.lock — run `proj init`")
    else:
        lock_specs = dbmod.specs_from_lock(lock)
        for record_type in lock_specs:
            if record_type not in load_registry(cfg.root):
                problems.append(f"lock declares record_type {record_type!r} missing from profiles/")

    db = rec.open_db(cfg)
    reg = load_registry(cfg.root)

    for record_type, spec in reg.items():
        if not db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (record_type,)).fetchone():
            problems.append(f"record_type {record_type!r}: no table in DB — run `proj init`")
            continue
        if not db.template_matches_table(spec):
            problems.append(f"record_type {record_type!r}: table schema does not match template "
                            f"(run `proj init` after template changes)")

        d = cfg.root / spec.dir
        seen: set[str] = set()
        # `_`-prefixed files are rendered views (e.g. _TOC.md), not records;
        # a missing dir is not a skip — orphaned DB rows still get reported
        md_files = (sorted(x for x in d.glob("*.md") if not x.name.startswith("_"))
                    if d.is_dir() else [])
        for p in md_files:
            text = p.read_text(encoding="utf-8")
            (mk, u), _title, raw_sections = rec.parse_record(text)
            if mk != record_type or not u:
                problems.append(f"{p.name}: missing/foreign uuid marker "
                                f"— run `proj adopt`")
                continue
            row = db.get(spec, u, by="uuid")
            if row is None:
                problems.append(f"{p.name}: uuid not in DB — run `proj adopt`")
                continue
            seen.add(row["uuid"])
            if not p.name.startswith(row["id"] + "-"):
                problems.append(f"{p.name}: id {row['id']!r} does not match filename")
            if spec.status_allowed and not any(
                    re.fullmatch(a.format(id=row["id"]), row["status"])
                    for a in spec.status_allowed):
                problems.append(f"{p.name}: status {row['status']!r} no longer allowed "
                                f"by the template ({', '.join(spec.status_allowed)})")
            db_sections = json.loads(row["sections"] or "{}")
            for s in spec.sections:
                if s.kind != "text":
                    continue
                stored = db_sections.get(s.name.lower(), "")
                if rec.normalize(raw_sections.get(s.name.lower(), "")) != stored:
                    problems.append(f"{p.name}: section '{s.name}' differs from DB "
                                    f"— run `proj sync`")
        for row in db.all(spec):
            if row["uuid"] not in seen:
                problems.append(f"{record_type} {row['id']}: record in DB has no md file")

    # per-record_type invariants: the record_type's Record owns its own checks
    for spec in reg.values():
        rows = db.all(spec)
        if rows:
            problems.extend(record_for(spec).check_rows(rows, db, reg))

    db.close()
    return problems
