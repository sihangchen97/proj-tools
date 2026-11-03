"""`proj check` — invariants across DB, lock, templates and md files."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.core.templates import load_registry
from proj.engine import store as rec
from proj.engine.record import record_for

# C6 content-only dump: the columns a full replay can actually reproduce.
# Dropped — an autoincrement `id` (re-insertion order differs),
# wip_entries.added_at and the F1/F2 file-fingerprint columns on records
# (runtime-only, a replay never writes them), and the incremental `ev:*` /
# fingerprint `fp:*` meta keys. Keep in step with compact._SELECT (the
# `proj compact` verify compares the same set).
_REPLAY_SELECT: dict[str, str] = {
    "records": ('SELECT id, type, title, status, summary, "commit", created, '
                'sections FROM records ORDER BY id'),
    "record_changelog": ("SELECT record_id, date, category, message, status, actor "
                         "FROM record_changelog ORDER BY record_id, date"),
    "items": "SELECT * FROM items ORDER BY record_id, no",
    "relations": "SELECT * FROM relations ORDER BY source, target, kind",
    "wip_entries": "SELECT record_id, pos, source FROM wip_entries "
                   "ORDER BY record_id",
    "changelog_entries": ("SELECT version, category, message, date FROM "
                          "changelog_entries ORDER BY version, category, message, date"),
}


def _norm_sections(s: str) -> str:
    """records.sections is a JSON STRING column — its key order is semantically
    irrelevant (the write path inserts sections in template order, the replayer
    in event order), so the comparison normalizes it to sorted-key form."""
    if not s or s == "{}":
        return s
    try:
        return json.dumps(json.loads(s), sort_keys=True, ensure_ascii=False)
    except json.JSONDecodeError as e:
        # P1-3: a corrupted sections column reports, doesn't crash check
        return f"<bad json: {e}>"



def _dump_state(db: dbmod.Db) -> dict:
    """The project state as comparable rows (see `_REPLAY_SELECT`)."""
    out = {t: [tuple(r) for r in db.conn.execute(q)]
           for t, q in _REPLAY_SELECT.items()}
    # normalize the records.sections JSON string (key order is not data)
    out["records"] = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                       _norm_sections(r[7])) for r in out["records"]]
    out["meta"] = [tuple(r) for r in db.conn.execute(
        "SELECT * FROM meta WHERE key NOT LIKE 'ev:%' AND key NOT LIKE 'fp:%' "
        "ORDER BY key")]
    return out


def _replay_consistent(cfg: ProjConfig, db: dbmod.Db) -> list[str]:
    """C6: the committed log must reproduce the cache (git collab P2): replay
    the whole events.jsonl onto an empty DB and compare the state. Called only
    when the file exists and passed the line-level validation above; an empty
    log is a no-op (nothing to reproduce yet)."""
    import sqlite3

    from proj import events as ev
    from proj import replay as rp
    events = ev.read(cfg.root / ".proj" / "events.jsonl")
    if not events:
        return []
    specs = load_registry(cfg.root)
    with tempfile.TemporaryDirectory() as td:
        fresh = dbmod.Db(Path(td) / "replay.sqlite")
        try:
            res = rp.replay(fresh, events, specs)
            before, after = _dump_state(db), _dump_state(fresh)
            diff = sorted(t for t in before if before[t] != after[t])
        except sqlite3.OperationalError as e:
            # the schema does not match the templates (reported above) — the
            # standard replay SELECT would not run; the consistency check is
            # meaningless on a mismatched schema
            return [f".proj/events.jsonl: replay consistency check skipped "
                    f"(schema mismatch: {e})"]
        except Exception as e:
            # P1-3: any other replay failure (a bad log, a schema drift the
            # OperationalError path missed) reports too — check never crashes
            return [f".proj/events.jsonl: replay consistency check "
                    f"skipped ({e!r})"]
        finally:
            fresh.close()
    problems = [f"events.jsonl replay: {d}" for d in res.diagnostics]
    if diff:
        problems.append(
            f".proj/events.jsonl: replaying the log does not reproduce the DB "
            f"(tables differ: {', '.join(diff)}) — the log is the source of "
            "truth: rebuild the cache from it (re-clone)")
    return problems


def check(cfg: ProjConfig) -> list[str]:
    problems: list[str] = []
    if not cfg.db_path.is_file():
        return ["no .proj/cache.sqlite — run `proj init`"]

    lock = dbmod.read_lock(cfg.root)
    if lock is None:
        problems.append("no .proj/lock.yaml — run `proj init`")
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
                try:
                    text = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as e:
                    # P1-3: a binary/corrupted file reports, doesn't crash
                    problems.append(f"{p.name}: unreadable ({e!r})")
                    continue
                (mk, u), _title, raw_sections = rec.parse_record(text)
                if mk != record_type or not u:
                    problems.append(f"{p.name}: missing/foreign id marker "
                                    f"— run `proj adopt`")
                    continue
                row = db.get(spec, u)
                if row is None:
                    problems.append(f"{p.name}: id not in DB — run `proj adopt`")
                    continue
                # R2: a renumbered record keeps its stable uuid under a new id, so
                # a file whose marker uuid differs from the record it claims is a
                # stale leftover (its old id is now held by another record) —
                # `sync` refuses to import it; check surfaces it.
                m_uuid = rec.marker_uuid(text)
                if m_uuid and m_uuid != (row.get("uuid") or ""):
                    problems.append(f"{p.name}: marker id {row['id']!r} is held by a "
                                    f"different record (stale copy from before a "
                                    f"renumber) — delete it, or `proj sync --rerender` "
                                    f"removes it")
                seen.add(row["id"])
                if not p.name.startswith(row["id"] + "-"):
                    problems.append(f"{p.name}: id {row['id']!r} does not match filename")
                if spec.status_allowed and not any(
                        re.fullmatch(a.format(id=row["id"]), row["status"])
                        for a in spec.status_allowed):
                    problems.append(f"{p.name}: status {row['status']!r} no longer allowed "
                                    f"by the template ({', '.join(spec.status_allowed)})")
                try:
                    db_sections = json.loads(row["sections"] or "{}")
                except json.JSONDecodeError as e:
                    # P1-3: a corrupted sections column reports, doesn't crash
                    problems.append(f"{p.name}: sections column is not valid "
                                    f"JSON ({e!r})")
                    db_sections = {}
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
        tspec = reg.get(row["type"])
        if tspec is None:
            # P1-3: a stale WIP row of a record type no longer in profiles/
            problems.append(f"WIP.md entry {row['id']}: record type "
                            f"{row['type']!r} is not in profiles/")
            continue
        if row["status"] in tspec.status_terminal:
            problems.append(
                f"WIP.md: {row['id']} is {row['status']!r} (terminal) — "
                f"remove it with `proj wip return {row['id']}`")

    # per-record_type invariants: the record_type's Record owns its own checks
    # (e.g. decision's supersede-cycle, plan's gate consistency)
    for spec in reg.values():
        rows = db.all(spec)
        if rows:
            problems.extend(record_for(spec).check_rows(rows, db, reg))

    # C6: item checklists reference tasks (→ T-…). Two cross-checks the per-type
    # check_rows do not cover — the `task` column was never validated:
    if "task" in reg:
        task_rows = db.all(reg["task"])
        task_ids = {r["id"] for r in task_rows}
        # expected item state per closed task (P3: task Done → done, Failed →
        # failed). An open task sets no expectation: a merge can leave an
        # item un-synced after its task closes, but a user may also pre-check
        # an item before its task is Done — so only flag the closed-task drift.
        task_state: dict[str, str] = {}
        for r in task_rows:
            if r["status"] in reg["task"].status_done:
                task_state[r["id"]] = "done"
            elif r["status"] in reg["task"].status_terminal:
                task_state[r["id"]] = "failed"
        for r in db.conn.execute(
                "SELECT record_id, no, state, task FROM items "
                "WHERE task!='' AND deleted=0"):
            if r["task"] not in task_ids:
                problems.append(
                    f"{r['record_id']} #{r['no']}: references task {r['task']!r}, "
                    f"which does not exist — run "
                    f"`proj item edit {r['record_id']}#{r['no']} --task ''`")
                continue
            want = task_state.get(r["task"])
            if want and r["state"] != want:
                problems.append(
                    f"{r['record_id']} #{r['no']}: references {r['task']} "
                    f"(now {want}) but is {r['state']!r} — a merge left it out of "
                    f"sync; run `proj item set {r['record_id']}#{r['no']} {want}`")

    # the event log (git collab E7): only checked when the file exists —
    # projects that have not started recording events are healthy without it.
    # In .proj/ (S.10); committed, so a corrupted one surfaces in `check`.
    events_path = cfg.root / ".proj" / "events.jsonl"
    if events_path.is_file():
        from proj import events  # lazy: events has no import cycle, kept late
        line_problems = events.validate(events_path)
        for msg in line_problems:
            problems.append(f".proj/events.jsonl: {msg}")
        # C6: only attempt the full replay when the log is well-formed (a
        # conflicted/unparseable line would abort it — already reported above).
        if not line_problems:
            problems.extend(_replay_consistent(cfg, db))

    db.close()
    return problems
