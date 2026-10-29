"""`proj check` — invariants across DB, lock, templates and md files."""
from __future__ import annotations

import json

from proj import db as dbmod
from proj import records as rec
from proj.config import ProjConfig
from proj.templates import load_registry


def check(cfg: ProjConfig) -> list[str]:
    problems: list[str] = []
    if not cfg.db_path.is_file():
        return ["no .proj/sqlite — run `proj init`"]

    lock = dbmod.read_lock(cfg.root)
    if lock is None:
        problems.append("no .proj.lock — run `proj init`")
    else:
        lock_specs = dbmod.specs_from_lock(lock)
        for kind in lock_specs:
            if kind not in load_registry(cfg.root):
                problems.append(f"lock declares kind {kind!r} missing from templates/")

    db = rec.open_db(cfg)
    reg = load_registry(cfg.root)

    for kind, spec in reg.items():
        if not db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (kind,)).fetchone():
            problems.append(f"kind {kind!r}: no table in DB — run `proj init`")
            continue
        if not db.template_matches_table(spec):
            problems.append(f"kind {kind!r}: table schema does not match template "
                            f"(run `proj init` after template changes)")

        d = cfg.root / spec.dir
        if not d.is_dir():
            continue
        seen: set[str] = set()
        for p in sorted(d.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            (mk, u), _title, raw_sections = rec.parse_record(text)
            if mk != kind or not u:
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
            db_sections = json.loads(row["sections"] or "{}")
            for s in spec.sections:
                if s.view:
                    continue
                stored = db_sections.get(s.name.lower(), "")
                if rec.normalize(raw_sections.get(s.name.lower(), "")) != stored:
                    problems.append(f"{p.name}: section '{s.name}' differs from DB "
                                    f"— run `proj sync`")
        for row in db.all(spec):
            if row["uuid"] not in seen:
                problems.append(f"{kind} {row['id']}: record in DB has no md file")

    # task: third-level nesting in Steps
    if "task" in reg:
        for r in db.all(reg["task"]):
            steps = json.loads(r["sections"] or "{}").get("steps", "")
            if any(i >= 2 for i, _, _ in rec.checkboxes(steps)):
                problems.append(f"task {r['id']}: Steps deeper than 2 levels "
                                f"— flatten sub-tasks")

    db.close()
    return problems
