"""Replay: the DB is a deterministic function of the event log (C3.1).

`replay(db, events, specs)` applies an event sequence to an EMPTY database
(the existing `Db` interface only — no business logic: no gates, no promote,
no P3 sync, per C2.1) and returns the notifications a merge surfaced:
record/item renumbering (G2), LWW field overwrites (G5), skipped dangling
references and unknown ops.

Determinism (P2): events are deduplicated by exact-line content and sorted
by `events.sort_key` before applying, so every clone replays the same lines
in the same order no matter how git interleaved them.

E2 stage (before C1 lands): the tables have no uuid columns yet — the
replayer keeps the record `uuid → id` mapping in memory, and item uuids ride
as a temporary `_uuid` field on the pending row dicts (the `items_save`
INSERT lists explicit columns, so it never reaches the DB). When C1 adds
the columns, one extra write per create/item.add persists the uuid.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from proj import events as ev
from proj.core import db as dbmod
from proj.core.templates import RecordTypeSpec

# `set` fields that live in the `records` table (C2.2: only changed columns)
_SET_FIELDS = ("title", "summary", "status", "commit")
# `item.set` fields that live in the `items` table
_ITEM_FIELDS = ("text", "state", "note", "owner", "due", "task")
# G5 simplification (C3.1): the same field changed by different actors within
# this many ms of each other is reported as an overwrite
_OVERWRITE_WINDOW_MS = 60_000
_CLOSED = ("done", "failed", "dropped")
# the changelog's Unreleased placeholder (profiles/changelog.yaml `unreleased`);
# a `changelog.add` event with an empty version means "Unreleased"
_UNRELEASED = "Unreleased"


class ReplayResult:
    """What a merge surfaced — printed by `proj sync` after a replay (C4)."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}        # record uuid -> final id
        self.renamed: list[dict] = []        # record or item renumbering (G2)
        self.overwrites: list[dict] = []     # LWW (G5): the overwritten value
        self.diagnostics: list[str] = []     # dangling refs, unknown ops, …


class _State:
    def __init__(self, specs: dict[str, RecordTypeSpec]) -> None:
        self.specs = specs
        self.ids: dict[str, str] = {}        # record uuid -> id
        self.types: dict[str, str] = {}      # record uuid -> record type
        self.rows: dict[str, list[dict]] = {}  # record id -> pending rows
        self.renamed: list[dict] = []
        self.overwrites: list[dict] = []
        self.diagnostics: list[str] = []
        # (uuid, field) -> (actor, at, value): the previous write, for G5
        self.last_write: dict[tuple, tuple] = {}
        # relations a snapshot re-inserts: buffered, flushed after every
        # record is re-created (a snapshot may point at a higher id)
        self.pending_rels: list = []

    # -- lookup ---------------------------------------------------------

    def rid(self, uuid: str) -> str:
        return self.ids.get(uuid, "")

    def spec(self, uuid: str) -> RecordTypeSpec | None:
        rtype = self.types.get(uuid)
        return self.specs.get(rtype) if rtype else None

    def rows_of(self, db, rid: str) -> list[dict]:
        if rid not in self.rows:
            self.rows[rid] = [dict(r) for r in db.items_load(rid)]
        return self.rows[rid]

    def row_by(self, rows: list[dict], item_uuid: str) -> dict | None:
        for r in rows:
            if r.get("_uuid") == item_uuid:
                return r
        return None

    def note_overwrite(self, e: dict, key: tuple, value: object) -> None:
        prev = self.last_write.get(key)
        if prev and prev[0] != e["actor"] and \
                0 <= e["at"] - prev[1] <= _OVERWRITE_WINDOW_MS:
            self.overwrites.append({
                "record": key[0], "field": key[1],
                "overwritten_value": prev[2], "overwritten_by": prev[0],
                "kept_value": value, "kept_by": e["actor"]})
        self.last_write[key] = (e["actor"], e["at"], value)


def replay(db, events: list[dict],
           specs: dict[str, RecordTypeSpec]) -> ReplayResult:
    """Apply `events` (any line order) to an empty `db`; return notifications."""
    # replay rebuilds a db from events — the write methods it drives must not
    # emit events back into the log (C2/C3: replay is read-side, not write-side)
    db.emitting = False
    # C3.1 steps 1-2: dedupe by exact line, then the total sort key
    seen: dict[str, dict] = {}
    for e in events:
        seen.setdefault(_line_sha(e), e)
    st = _State(specs)
    ordered = sorted(seen.values(), key=ev.sort_key)
    for e in ordered:  # E6: reserve create/snapshot ids in sorted order so a
        if e["op"] in ("create", "snapshot") and e.get("rec"):  # snapshot's
            _pre_alloc(st, e)                                   # relation can
    for e in ordered:                                           # point earlier
        _apply(st, db, e)
    for rid, dst, kind in st.pending_rels:  # snapshots: all records exist now
        db.add_relation(rid, dst, kind)
    for rid in st.rows:
        db.items_save(rid, st.rows[rid])
    res = ReplayResult()
    res.ids = dict(st.ids)
    res.renamed = st.renamed
    res.overwrites = st.overwrites
    res.diagnostics = st.diagnostics
    return res


def _line_sha(e: dict) -> str:
    return hashlib.sha1(ev.to_line(e).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------


def _apply(st: _State, db, e: dict) -> None:
    op, rec = e["op"], e.get("rec", "")
    if op == "create":
        spec = st.specs.get(e["type"])
        if spec is None:
            st.diagnostics.append(f"create: unknown record type {e['type']!r}")
            return
        # id + rename already decided by the pre-pass (sorted order, so a
        # collision is resolved deterministically); a create without a
        # pre-allocation (defensive) still allocates on the spot
        rid = st.ids.get(rec) or _alloc_id(st, spec, rec, e["id"])
        st.ids[rec] = rid
        st.types[rec] = e["type"]
        db.insert(spec, rid, e["title"], e["status"],
                  e.get("created") or dbmod.utc_now(),
                  summary=e.get("summary", ""), commit=e.get("commit", ""),
                  uuid=rec)
        sections = e.get("sections") or {}
        if sections:
            db.set(spec, rid, sections=json.dumps(sections, ensure_ascii=False))
        return
    # project-level ops: no record (rec == ""), before the rid/spec lookup
    if op == "changelog.add":
        # an empty version means the Unreleased placeholder (the DB's value)
        db.add_cl_entry(e.get("version") or _UNRELEASED,
                        e.get("category", "Added"), e.get("message", ""),
                        when=e.get("date"))
        return
    if op == "changelog.release":
        # stamp the Unreleased batch with the released version (mirrors
        # views.release's UPDATE ... WHERE version = cl.unreleased)
        db.conn.execute(
            "UPDATE changelog_entries SET version=? WHERE version=?",
            (e["version"], _UNRELEASED))
        return
    rid = st.rid(rec)
    spec = st.spec(rec)
    if (not rid or spec is None) and not (op == "snapshot" and not rec):
        st.diagnostics.append(f"{op}: unknown record uuid {rec!r}")
        return
    if op == "set":
        fields = {k: v for k, v in e.get("fields", {}).items()
                  if k in _SET_FIELDS}
        for k in fields:
            st.note_overwrite(e, (rec, k), fields[k])
        if fields:
            db.set(spec, rid, **fields)
    elif op == "section.set":
        row = _row(st, db, rid)
        sections = json.loads(row["sections"] or "{}")
        sections[e["name"]] = e.get("text", "")
        db.set(spec, rid, sections=json.dumps(sections, ensure_ascii=False))
    elif op == "log":
        db.conn.execute(
            "INSERT INTO record_changelog (record_id, date, category, "
            "message, status, actor) VALUES (?,?,?,?,?,?)",
            (rid, e.get("date") or dbmod.utc_now(),
             e.get("category", "Changed"), e.get("message", ""),
             e.get("status", ""), e.get("actor", "")))
    elif op == "delete":
        db.delete(spec, rid)  # items + relations cascade (FK), WIP below
        db.conn.execute("DELETE FROM wip_entries WHERE record_id=?", (rid,))
        st.rows.pop(rid, None)
    elif op in ("item.add", "item.set", "item.move", "group.add",
                "group.set", "group.move"):
        _apply_item(st, db, e)
    elif op in ("rel.add", "rel.del"):
        dst = st.rid(e["target"])
        if not dst:
            st.diagnostics.append(
                f"{op}: target {e.get('id') or e['target']!r} (uuid "
                f"{e['target']}) does not exist - skipped")
            return
        (db.add_relation if op == "rel.add" else db.remove_relation)(
            rid, dst, e["kind"])
    elif op == "wip.add":
        pos = db.conn.execute(
            "SELECT COALESCE(MAX(pos), -1) + 1 FROM wip_entries").fetchone()[0]
        db.conn.execute(
            "INSERT OR IGNORE INTO wip_entries (record_id, pos, added_at, "
            "source) VALUES (?,?,?,?)",
            (rid, pos, e.get("added_at") or dbmod.utc_now(),
             e.get("source", "manual")))
    elif op == "wip.del":
        db.wip_remove(rid)
    elif op == "snapshot":
        _apply_snapshot(st, db, e)
        return
    else:
        st.diagnostics.append(f"unknown op {op!r}")


def _row(st: _State, db, rid: str) -> dict:
    return dict(db.conn.execute("SELECT * FROM records WHERE id=?",
                                (rid,)).fetchone())


# -- records -----------------------------------------------------------------


def _alloc_id(st: _State, spec: RecordTypeSpec, uuid: str,
              proposed: str) -> str:
    """G2: a proposed id already held by ANOTHER uuid takes the next free
    number of that type (deterministic — everyone replays the same order)."""
    clash = next((u for u, i in st.ids.items() if i == proposed), "")
    if clash and clash != uuid:
        n = max((int(i.split("-", 1)[1]) for i in st.ids.values()
                 if i.startswith(spec.code + "-")
                 and i.split("-", 1)[1].isdigit()), default=0)
        while f"{spec.code}-{n + 1:0{spec.numbered}d}" in set(st.ids.values()):
            n += 1
        gid = f"{spec.code}-{n + 1:0{spec.numbered}d}"
        st.renamed.append({"uuid": uuid, "proposed": proposed, "actual": gid,
                           "collides_with": clash})
        return gid
    return proposed


# -- items -------------------------------------------------------------------


def _letter(no: str) -> str:
    return no[0] if no[:1].isalpha() else ""


def _next_no(rows: list[dict], letter: str) -> str:
    """The next free number of this group in this record — numbering is
    shared record-wide, tombstones included (C3.1 / §4.3)."""
    used = {r["no"] for r in rows}
    cap = 99 if letter else 999
    for n in range(1, cap + 1):
        no = f"{letter}{n:02d}" if letter else f"{n:03d}"
        if no not in used:
            return no
    raise ValueError(f"no free number left in group {letter or '∅'} (cap {cap})")


def _task_id(st: _State, db, e: dict, task_uuid: str) -> str:
    """The item's task ref: events carry the target's uuid (C1); the DB keeps
    ids. Unknown target -> empty + diagnostic (C3.1)."""
    if not task_uuid:
        return ""
    tid = st.rid(task_uuid)
    if not tid or st.types.get(task_uuid) != "task":
        st.diagnostics.append(
            f"item task ref {e.get('id') or task_uuid!r} (uuid {task_uuid}) "
            "does not exist (or is not a task) — skipped")
        return ""
    return tid


def _after_index(st: _State, rows: list[dict], after_uuid: str) -> int:
    """Display index to insert after (the `after` item's uuid); a missing
    target goes to the end of the checklist (C3.1)."""
    if not after_uuid:
        return len(rows)  # no anchor: append
    row = st.row_by(rows, after_uuid)
    if row is None:
        st.diagnostics.append(
            f"after-ref (uuid {after_uuid}) does not exist — appended at the end")
        return len(rows)
    return rows.index(row) + 1


def _apply_item(st: _State, db, e: dict) -> None:
    rid = st.rid(e["rec"])
    if not rid:
        st.diagnostics.append(f"{e['op']}: unknown record uuid {e['rec']!r}")
        return
    rows = st.rows_of(db, rid)
    op, iu = e["op"], e["item"]
    if op in ("item.add", "group.add"):
        no = e["no"]
        letter = _letter(no)
        if op == "group.add" and not no:
            # a fresh group: the next free letter (A-Z, never reused)
            letter = _next_group(rows)
            no = f"{letter}00"
        if no in {r["no"] for r in rows}:
            no = _next_no(rows, letter)
            st.renamed.append({"item": iu, "proposed": e["no"],
                               "actual": no, "record": rid})
        rows.insert(_after_index(st, rows, e.get("after", "")), {
            "_uuid": iu, "no": no, "text": e.get("text", ""),
            "state": e.get("state", "open"), "note": e.get("note", ""),
            "owner": e.get("owner", ""), "due": e.get("due", ""),
            "commit": e.get("commit", ""), "trailer": e.get("trailer", ""),
            "created": e.get("created") or dbmod.utc_now(),
            "closed_at": e.get("closed_at", ""), "deleted": 0,
            "section": e.get("section", ""),
            "task": _task_id(st, db, e, e.get("task", ""))})
        return
    row = st.row_by(rows, iu)
    if row is None:
        st.diagnostics.append(
            f"{op}: item {e.get('id') or iu!r} (uuid {iu}) not found - skipped")
        return
    if op in ("item.set", "group.set"):
        for k, v in e.get("fields", {}).items():
            if k in _ITEM_FIELDS:
                val = _task_id(st, db, e, v) if k == "task" else v
                st.note_overwrite(e, (rid, f"#{row['no']}.{k}"), val)
                row[k] = val
            elif k == "deleted":
                row["deleted"] = 1 if v else 0
                if not v:  # an explicit un-delete clears the close (C3.1)
                    row["closed_at"] = ""
        if op == "item.set" and row.get("state") in _CLOSED and not row["closed_at"]:
            row["closed_at"] = e.get("date") or dbmod.utc_now()
        return
    # item.move / group.move: a new position in the checklist; changing the
    # group (letter) or section renumbers and keeps the old number as a
    # tombstone - exactly like the command's behavior (C3.1 / S5.2).
    # C2: `pos` (the target index in the display list) is exact and can
    # express "move to the front" (an `after` anchor cannot - after="" means
    # the END); when `pos` is absent the legacy `after` anchor is used.
    rows.remove(row)
    target_letter = _letter(e["no"]) if e.get("no") else _letter(row["no"])
    new_section = e.get("section", row["section"])
    if target_letter != _letter(row["no"]) or new_section != row["section"]:
        no = _next_no(rows, target_letter)
        rows.append({**row, "_uuid": None, "deleted": 1})
        st.renamed.append({"item": iu, "proposed": row["no"],
                           "actual": no, "record": rid})
        row["no"] = no
    row["section"] = new_section
    if e.get("pos") is not None:
        rows.insert(min(max(int(e["pos"]), 0), len(rows)), row)
    else:
        rows.insert(_after_index(st, rows, e.get("after", "")), row)


def _next_group(rows: list[dict]) -> str:
    """The next free group letter (the max existing letter + 1; tombstones
    count too, so letters are never reused - S5.2 / the model's rule)."""
    used = {r["no"][0] for r in rows if r["no"][:1].isalpha()}
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if c not in used:
            return c
    raise ValueError("no free group letter left (A-Z exhausted)")

# ---------------------------------------------------------------------------
# E5 (C3.2): incremental replay — the judgment + the application. Wiring into
# the command entry (write lock, same-transaction meta update) is alex's.


def load_meta(db) -> dict:
    """The `ev:*` keys from the meta table ({} if none yet): ev:size,
    ev:prefix_hash (sha1 of the first ev:size bytes), ev:last_key (the max
    sort key applied so far, as a JSON list), ev:uuids (uuid -> id bridge
    until C1 adds uuid columns)."""
    out = {}
    for k, raw_v in db.conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'ev:%'"):
        v = raw_v
        if k == "ev:last_key":
            v = tuple(json.loads(raw_v)) if raw_v else None
        elif k in ("ev:uuids", "ev:types"):
            v = json.loads(raw_v) if raw_v else {}
        out[k] = v
    return out


def save_meta(db, meta: dict) -> None:
    for k, raw_v in meta.items():
        if not k.startswith("ev:"):
            continue
        v = raw_v
        if k == "ev:last_key":
            v = json.dumps(list(raw_v)) if raw_v is not None else ""
        elif k in ("ev:uuids", "ev:types"):
            v = json.dumps(raw_v, ensure_ascii=False)
        db.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    db.conn.commit()


def commit_meta(db, path) -> dict:
    """Recompute ev:size / ev:prefix_hash / ev:last_key from the current
    event file and write them (the caller holds the write lock; C3.2: local
    appends update the meta in the same transaction so they never trigger a
    replay). ev:uuids is untouched."""
    p = Path(path)
    raw = p.read_bytes() if p.is_file() else b""
    meta = load_meta(db)
    meta["ev:size"] = len(raw)
    meta["ev:prefix_hash"] = hashlib.sha1(raw).hexdigest()
    lines = [ln for ln in raw.split(b"\n") if ln]
    if lines:
        evs = [ev.from_line(ln.decode("utf-8")) for ln in lines]
        meta["ev:last_key"] = max(ev.sort_key(e) for e in evs)
    save_meta(db, meta)
    return meta


def rebuild_cache(cache_path, event_path,
                  specs: dict[str, RecordTypeSpec]) -> ReplayResult:
    """C3.1 step 3 / C4: replay the WHOLE event log onto a fresh database and
    atomically replace the cache (`.proj/cache.sqlite` ← temp + os.replace).
    Used when the cache is missing (a clone's first command, or the cache was
    deleted) or the incremental judgment said "full". Returns the
    notifications (renumbering / overwrites / diagnostics) — the caller stores
    them in meta (ev:notifications) so `proj sync` can report them."""
    import os
    p = Path(cache_path)
    tmp = p.parent / "cache.sqlite.tmp"
    for suffix in ("", "-wal", "-shm"):
        f = p.parent / (tmp.name + suffix)
        if f.exists():
            f.unlink()
    db = dbmod.Db(tmp)
    try:
        db.emitting = False  # replay is read-side; its writes emit no events
        events = ev.read(event_path) if Path(event_path).is_file() else []
        res = replay(db, events, specs)
        # C3.2: the cache now covers the whole file — reset the bookkeeping
        meta = commit_meta(db, event_path)
        meta["ev:notifications"] = json.dumps(
            {"renamed": res.renamed, "overwrites": res.overwrites,
             "diagnostics": res.diagnostics}, ensure_ascii=False)
        meta["ev:rebuilt_at"] = dbmod.utc_now()  # C4: `proj info` shows it
        save_meta(db, meta)
        db.conn.commit()
        # WAL mode: data lives in cache.sqlite.tmp-wal until checkpointed into
        # the main file. Force it in BEFORE the replace (otherwise the main
        # file is empty and the WAL is orphaned).
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        db = None
        os.replace(tmp, p)  # atomic: readers see either the old or the new cache
    except Exception:
        if db is not None:
            try: db.close()
            except Exception: pass
        for suffix in ("", "-wal", "-shm"):
            f = p.parent / (tmp.name + suffix)
            if f.exists(): f.unlink()
        raise
    return res


def apply_and_store(db, res: ReplayResult, event_path) -> None:
    """C3.2 append path: persist the incremental result (the uuid bridge + the
    fresh bookkeeping over the now-longer file) into the EXISTING cache, plus
    any notifications the appended lines surfaced (so `proj sync` reports them)."""
    m = commit_meta(db, event_path)
    m["ev:uuids"] = dict(res.ids)
    if res.renamed or res.overwrites or res.diagnostics:
        m["ev:notifications"] = json.dumps(
            {"renamed": res.renamed, "overwrites": res.overwrites,
             "diagnostics": res.diagnostics}, ensure_ascii=False)
    save_meta(db, m)
    db.conn.commit()


def plan_incremental(path, meta: dict):
    """C3.2 judgment: ("noop", None) | ("append", [parsed new lines]) |
    ("full", reason). Pure read of the file — no db, no mutation.

    - size == ev:size                    -> noop (one stat, C3.3)
    - size < ev:size (truncated)         -> full
    - file does not end with \\n         -> full (a fused last line)
    - first ev:size bytes changed (rebase/revert) -> full
    - any new line unparseable           -> full
    - any new line's sort key <= ev:last_key (an earlier line was inserted,
      e.g. by a merge) -> full
    - otherwise (a plain append, all later) -> ("append", the new lines)
    """
    p = Path(path)
    size = 0 if not p.is_file() else p.stat().st_size
    seen = int(meta.get("ev:size", 0) or 0)
    if size == seen:
        return ("noop", None)
    if size < seen:
        return ("full", "the event file shrank (truncation or rewrite)")
    raw = p.read_bytes()
    if not raw.endswith(b"\n"):
        return ("full", "the event file does not end with a newline")
    if hashlib.sha1(raw[:seen]).hexdigest() != meta.get("ev:prefix_hash", ""):
        return ("full", "the prefix changed (rebase, revert or edit)")
    new_lines = [ln for ln in raw[seen:].split(b"\n") if ln]
    parsed = []
    for ln in new_lines:
        try:
            parsed.append(ev.from_line(ln.decode("utf-8")))
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            return ("full", f"a new line does not parse ({e})")
    last = meta.get("ev:last_key")
    if last is not None and any(ev.sort_key(e) <= last for e in parsed):
        return ("full", "a new line sorts before the last applied key "
                        "(an earlier event was inserted)")
    return ("append", parsed)


def apply_incremental(db, lines: list[dict],
                      specs: dict[str, RecordTypeSpec]) -> ReplayResult:
    """Apply only the new (already-later) lines to the EXISTING cache db.
    The record uuid -> id map is seeded from the `records` table (C1 made
    uuid a column — the meta bridge is redundant and never read as a source)
    and merged back afterwards. Items of existing records are loaded from the
    db, so this never rewrites untouched checklists."""
    st = _State(specs)
    for r in db.conn.execute("SELECT id, uuid, type FROM records"):
        if r["uuid"]:
            st.ids[r["uuid"]] = r["id"]
            st.types[r["uuid"]] = r["type"]
    for e in sorted(lines, key=ev.sort_key):
        _apply(st, db, e)
    for rid in st.rows:
        db.items_save(rid, st.rows[rid])
    res = ReplayResult()
    res.ids = dict(st.ids)
    res.renamed = st.renamed
    res.overwrites = st.overwrites
    res.diagnostics = st.diagnostics
    return res

# ---------------------------------------------------------------------------
# E6 (C6 compact): snapshots — one event per record + a project-level one,
# whose replay yields the same tables as the original log.


def _pre_alloc(st: _State, e: dict) -> None:
    """A create or snapshot reserves its id before any op is applied, in
    sorted order (the only order replay uses): a snapshot's relation can
    point at a record whose snapshot line comes later in the file."""
    rec = e.get("rec", "")
    if not rec or rec in st.ids:
        return
    spec = st.specs.get(e["type"])
    if spec is None:
        return
    st.ids[rec] = _alloc_id(st, spec, rec, e["id"])
    st.types[rec] = e["type"]


def _apply_snapshot(st: _State, db, e: dict) -> None:
    rec, rid = e.get("rec", ""), st.rid(e.get("rec", ""))
    if not rec:  # project-level: replace the whole changelog
        db.conn.execute("DELETE FROM changelog_entries")
        for ce in e.get("changelog", []):
            db.conn.execute(
                "INSERT INTO changelog_entries (version, category, message, "
                "date) VALUES (?,?,?,?)",
                (ce["version"], ce["category"], ce["message"], ce["date"]))
        return
    spec = st.spec(rec)
    if not rid or spec is None:
        st.diagnostics.append(f"snapshot: unknown record uuid {rec!r}")
        return
    # replace: drop the current state (items/relations cascade, WIP below)
    db.delete(spec, rid)
    db.conn.execute("DELETE FROM wip_entries WHERE record_id=?", (rid,))
    st.rows.pop(rid, None)
    db.insert(spec, rid, e["title"], e["status"],
              e.get("created") or dbmod.utc_now(),
              summary=e.get("summary", ""), commit=e.get("commit", ""),
              uuid=rec)
    sections = e.get("sections") or {}
    if sections:
        db.set(spec, rid, sections=json.dumps(sections, ensure_ascii=False))
    for lg in e.get("logs", []):
        db.conn.execute(
            "INSERT INTO record_changelog (record_id, date, category, "
            "message, status, actor) VALUES (?,?,?,?,?,?)",
            (rid, lg["date"], lg["category"], lg["message"], lg["status"],
             lg["actor"]))
    for r in e.get("relations", []):
        dst = st.rid(r["target"])
        if not dst:
            st.diagnostics.append(
                f"snapshot: relation target {r['target']!r} not in the "
                "snapshot set - skipped")
            continue
        st.pending_rels.append((rid, dst, r["kind"]))
    if e.get("wip"):
        db.wip_add(rid, e.get("source", "manual"))
    rows = e.get("items", [])
    if rows:
        # rows in their stored display order; pos is the list index again
        st.rows[rid] = [dict(r) for r in rows]
        # _uuid fields ride as before (temporary, never persisted, E2 stage)


def snapshot_events(db, at: int | None = None, actor: str = "compact") -> list:
    """C6: the state of a replayed db as one `snapshot` event per record (in
    id order) + one project-level `snapshot` (the changelog). Compact (wired
    by alex in C6) rewrites events.jsonl with exactly these events, the sort
    key taken from the original maximum, then replays and compares.

    C1: the db carries the uuid columns — `rec` is the record's stored uuid
    (records.uuid / items.uuid), and the snapshot's items ride their uuid in
    the `uuid` field (items_save persists it)."""
    at = at or int(time.time() * 1000)
    out: list = []
    for r in db.conn.execute("SELECT * FROM records ORDER BY id"):
        rid = r["id"]
        uuid = r["uuid"]
        sections = json.loads(r["sections"] or "{}")
        # C1: the snapshot's relation targets ride as uuids (st.rid resolves
        # them), not raw ids — the id of every referenced record
        id_of = {row["id"]: row["uuid"] for row in
                 db.conn.execute("SELECT id, uuid FROM records")}
        rels = [{"target": id_of[row["target"]], "kind": row["kind"]}
                for row in db.conn.execute(
                    "SELECT target, kind FROM relations WHERE source=? "
                    "ORDER BY target, kind", (rid,))]
        wip = db.conn.execute(
            "SELECT source FROM wip_entries WHERE record_id=?", (rid,)).fetchone()
        out.append(ev.make(
            "snapshot", actor=actor, at=at, seq=len(out), rec=uuid,
            type=r["type"], id=rid, title=r["title"], status=r["status"],
            summary=r["summary"], commit=r["commit"], created=r["created"],
            sections=sections, relations=rels,
            logs=[{"date": r["date"], "category": r["category"],
                   "message": r["message"], "status": r["status"],
                   "actor": r["actor"]}
                   for r in reversed(db.logs(rid))],  # chronological order
            items=[dict(row) for row in db.items_load(rid)],
            wip=bool(wip), source=wip[0] if wip else ""))
    out.append(ev.make(
        "snapshot", actor=actor, at=at, seq=len(out), rec="",
        changelog=[{"version": r["version"], "category": r["category"],
                    "message": r["message"], "date": r["date"]}
                   for r in db.cl_entries()]))
    return out
