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

    db = rec.open_db(cfg)  # R3: check is read-only; the context closes it
    reg = load_registry(cfg.root)

    if not db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
        problems.append("no records table in DB — run `proj init`")
    else:
        for record_type, spec in reg.items():
            if not db.template_matches_table(spec):
                problems.append(
                    f"record_type {record_type!r}: table schema does not match template "
                    "(run `proj init` after template changes)")

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
                    problems.append(f"{p.name}: missing/foreign id marker "
                                    f"— run `proj adopt`")
                    continue
                row = db.get(spec, u)
                if row is None:
                    problems.append(f"{p.name}: id not in DB — run `proj adopt`")
                    continue
                seen.add(row["id"])
                if not p.name.startswith(row["id"] + "-"):
                    problems.append(f"{p.name}: id {row['id']!r} does not match filename")
                if spec.status_allowed and not any(
                        re.fullmatch(a.format(id=row["id"]), row["status"])
                        for a in spec.status_allowed):
                    problems.append(f"{p.name}: status {row['status']!r} no longer allowed "
                                    f"by the template ({', '.join(spec.status_allowed)})")
                db_sections = json.loads(row["sections"] or "{}")
                # P1: every checklist section renders entirely from the DB state
                item_secs = {it.section for it in spec.items}
                for s in spec.sections:
                    if s.kind != "text":
                        continue
                    if s.name in item_secs:
                        from proj.engine import items as items_mod  # lazy
                        if items_mod.drift(db, spec, row,
                                           raw_sections.get(s.name.lower(), ""),
                                           section=s.name):
                            problems.append(f"{p.name}: section '{s.name}' (items) differs "
                                            f"from DB — run `proj sync`")
                        continue
                    stored = db_sections.get(s.name.lower(), "")
                    if rec.normalize(raw_sections.get(s.name.lower(), "")) != stored:
                        problems.append(f"{p.name}: section '{s.name}' differs from DB "
                                        f"— run `proj sync`")
                # per-checklist gate (P1): a record in one of a checklist's gate
                # statuses must have that checklist fully closed
                for it in spec.items:
                    if row["status"] not in it.gate:
                        continue
                    from proj.engine import items as items_mod  # lazy
                    state = items_mod.load(db, row["id"], it)
                    state.intro = items_mod._intro_of(it, row)
                    un = items_mod.open_items(state, it)
                    if un:
                        problems.append(
                            f"{p.name}: {spec.record_type} {row['id']} is {row['status']!r} "
                            f"(in the '{it.section}' gate) but has unclosed {it.noun}(s): "
                            f"{', '.join(un)}")
            for row in db.all(spec):
                if row["id"] not in seen:
                    problems.append(f"{record_type} {row['id']}: record in DB has no md file")

    # WIP.md invariants (plans §5.5): the rendered file matches the DB, and no
    # terminal-status record sits in wip_entries (auto-return missed it)
    from proj.engine import wip as wipmod  # lazy: check↔wip cycle
    expected = wipmod.render_wip_md(cfg, db, reg).text
    wip_path = cfg.root / wipmod.WIP_FILE
    if not wip_path.is_file():
        problems.append("WIP.md missing — run `proj render`")
    elif wip_path.read_text(encoding="utf-8") != expected:
        problems.append("WIP.md differs from the DB render — run `proj sync`")
    for entry in db.wip_all():
        row = db.conn.execute(
            "SELECT * FROM records WHERE id=?", (entry["record_id"],)).fetchone()
        if row is None:
            problems.append(f"WIP.md entry {entry['record_id']}: record not in DB")
            continue
        if row["status"] in reg[row["type"]].status_terminal:
            problems.append(
                f"WIP.md: {row['id']} is {row['status']!r} (terminal) — "
                f"remove it with `proj wip return {row['id']}`")

    # per-record_type invariants: the record_type's Record owns its own checks
    # (e.g. decision's supersede-cycle, plan's gate consistency)
    for spec in reg.values():
        rows = db.all(spec)
        if rows:
            problems.extend(record_for(spec).check_rows(rows, db, reg))

    # the event log (git collab E7): only checked when the file exists —
    # projects that have not started recording events are healthy without it.
    # S.10 moves the file into .proj/; until then it sits next to the DB.
    events_path = cfg.root / "events.jsonl"
    if events_path.is_file():
        from proj import events  # lazy: events has no import cycle, kept late
        for msg in events.validate(events_path):
            problems.append(f"events.jsonl: {msg}")

    db.close()
    return problems
