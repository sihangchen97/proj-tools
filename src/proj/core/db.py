"""SQLite core (.proj/cache.sqlite) — the single source of truth.

Unified tables (schema v4):

    records(id PK, type, title, status, summary, commit, created, sections JSON)
    record_changelog(id PK, record_id, date, category, message, status)
    relations(source, target, kind) — PK (source, target, kind); one-parent unique index

record_changelog: every row stores the record's status AFTER that event
(`status` = the new state on a status change, the unchanged state on any
other event), so a changelog line renders as `- `date - emoji status``;
`message` is the optional free text (--message, git-style).

Plus the global changelog:
    changelog_entries(id PK, version, category, message, date)

The lock file (.proj/lock.yaml, S.10) pins which templates built the DB so a fresh
clone without the DB can rebuild the same schema.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid as uuid_mod
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proj.core.templates import ChangelogSpec

import yaml

from proj import events as ev
from proj.core.config import reporter
from proj.core.templates import ItemsSpec, RecordTypeSpec

SCHEMA_VERSION = 11  # C1: + records.uuid / items.uuid (event-log identity)
#: the profile lock (S.10): committed, in the .proj/ data directory
LOCK_FILE = ".proj/lock.yaml"

CATEGORY_RE = re.compile(r"^([A-Za-z][A-Za-z ]*):\s*(.+)$")


class ProjError(ValueError):
    """Base for proj tool errors (A4/D4). MCP's _safe and the CLI catch it and
    return a clean 'error:' string instead of a raw traceback. ItemsError
    (engine/items.py) and friends subclass it."""


NETWORK_FS_TYPES = ("nfs", "nfs4", "cifs", "smb", "smbfs", "fuse.sshfs")


def _mounts_lines() -> list[str]:
    try:
        return Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return []


def on_network_fs(path: Path) -> bool:
    """True when `path` lives on a network filesystem (Linux /proc/mounts).
    SQLite's WAL mode relies on shared memory and must not be used on NFS:
    two hosts writing the same project could corrupt the DB. Unknown platform
    (no /proc/mounts) → False (keep WAL)."""
    mounts = _mounts_lines()
    if not mounts:
        return False
    target = str(path.resolve())
    best = ""
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fs_type = parts[1], parts[2]
        if (fs_type in NETWORK_FS_TYPES
                and (mount_point == target or target.startswith(mount_point + "/"))
                and len(mount_point) > len(best)):
            best = mount_point
    return bool(best)


def utc_now() -> str:
    """D10: timestamps are stored as UTC ISO8601 (second precision, sortable);
    the DATE part is what gets rendered (show_date)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def show_date(ts: str) -> str:
    """The date part of a stored timestamp ('2026-10-31T08:23:11+00:00' →
    '2026-10-31'); plain dates pass through ([:10] is idempotent)."""
    return (ts or "")[:10]


class Db:
    def __init__(self, path: Path):
        self.path = path
        # X0: change tracking for the current transaction — the record ids
        # written (any write method) and the rendered views touched
        # ("wip" / "changelog"); cleared on rollback and at the end of a
        # committed `with` (the next command starts clean). F6 renders from
        # this set; C2 feeds events from the _emit hooks (X0: no-op).
        self.touched: set[str] = set()
        self.touched_views: set[str] = set()
        # C2: the events of the current write transaction, accumulated in
        # _emit; flushed to .proj/events.jsonl with ONE os.write BEFORE the
        # DB commit (the log is authoritative — C2.3). Rollback drops them.
        self._events: list[dict] = []
        self._seq = 0
        self._events_flushed = False
        self._rolled_back = False
        # C3: replay (replay.py) drives the write methods to rebuild a
        # throwaway db from events — it must NOT emit events back into
        # the log (that would re-append what it just read). Replay sets
        # `emitting = False`; normal write commands keep it True.
        self.emitting = True
        # §13.2: WAL needs shared memory and must not be used on network
        # filesystems — fall back to the rollback journal there (one writer at a
        # time; busy_timeout still serializes them).
        self.network_fs = on_network_fs(path)
        journal_mode = "DELETE" if self.network_fs else "WAL"
        # busy_timeout: several agents may share the project db; a reader no
        # longer fails with "database is locked" while another writes.
        # R3/N1: isolation_level=None (autocommit) — write transactions are
        # explicit (BEGIN IMMEDIATE in __enter__, one per command); non-context
        # callers keep legacy per-statement commits (read-only / one statement).
        self.conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA journal_mode = {journal_mode}")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        # C2.2: HLC — `at = max(now, last seen) + 1ms`; the last-seen value
        # lives in meta `ev:last_at` so an actor's later events always sort
        # after the ones they have seen (a skewed clock is safe)
        last_at = 0
        row = self.conn.execute("SELECT value FROM meta WHERE key='ev:last_at'").fetchone()
        if row:
            try:
                last_at = int(row["value"])
            except ValueError:
                last_at = 0
        self._hlc = ev.HlcClock(last_at)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS changelog_entries "
            "(id INTEGER PRIMARY KEY, version TEXT, category TEXT, message TEXT, date TEXT)")
        # inter-record relations: one row per edge, no id column; PK
        # (source, target, kind) — the same record pair may hold several edge
        # kinds (e.g. parent + related) at once.
        # kind "supersede": same-type; "related": any type, unordered; "parent":
        # source=parent, target=child task; a task has at most ONE parent edge
        # (the unique index below).
        # D8: no type columns — the record type resolves from the id prefix
        # (registry code→type); FK CASCADE deletes edges with their record.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS relations "
            "(source TEXT NOT NULL "
            "REFERENCES records(id) ON DELETE CASCADE, "
            "target TEXT NOT NULL "
            "REFERENCES records(id) ON DELETE CASCADE, "
            "kind TEXT NOT NULL, "
            "PRIMARY KEY (source, target, kind))")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS relations_one_parent "
            "ON relations(target) WHERE kind='parent'")
        # unified record storage: one table per type was replaced by a type column
        # F1: + the md file's fingerprint columns (local cache only — never part
        # of the data: no event, no --json, no lock)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS records "
            "(id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, status TEXT, "
            "summary TEXT, \"commit\" TEXT, created TEXT, sections TEXT, "
            "md_mtime_ns INTEGER, md_size INTEGER, md_hash TEXT, "
            "uuid TEXT NOT NULL DEFAULT '')")
        # F1: pre-F1 databases (schema < 10) lack the fingerprint columns —
        # add them in place (pure cache, NULL = "no fingerprint yet": the first
        # read of a file fills it in).
        for col, dtype in (("md_mtime_ns", "INTEGER"), ("md_size", "INTEGER"),
                          ("md_hash", "TEXT"), ("uuid", "TEXT")):
            if col not in self.table_columns("records"):
                self.conn.execute(
                    f"ALTER TABLE records ADD COLUMN {col} {dtype}"
                    + (" NOT NULL DEFAULT ''" if col == "uuid" else ""))
                self.conn.commit()  # DDL is not transactional in SQLite
                # C1: existing rows get a fresh uuid (events for them start after
                # this migration)
                if col == "uuid":
                    for r in self.conn.execute("SELECT id FROM records WHERE uuid=''",):
                        self.conn.execute(
                            "UPDATE records SET uuid=? WHERE id=?",
                            (uuid_mod.uuid4().hex, r[0]))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS record_changelog "
            "(id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, date TEXT, "
            "category TEXT, message TEXT, status TEXT, "
            "actor TEXT NOT NULL DEFAULT '')")  # S.4: who made the change
        # item checklists (items.py): ONE table. `no` is the 3-char number:
        # '001'..'999' ungrouped, 'A01'..'Z99' inside group A (the first letter IS
        # the group), 'A00' = group A itself (its text is the group name). Deleted
        # numbers stay as tombstones (deleted=1) so numbers are never reused;
        # groups have no counter table either (the max 'X00' wins).
        # pos = display order of the whole checklist.
        # FK CASCADE deletes items with their record (no manual cleanup).
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS items "
            "(record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE, "
            "no TEXT NOT NULL, pos INTEGER NOT NULL, "
            "text TEXT NOT NULL, state TEXT NOT NULL DEFAULT '', "
            "note TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '', "
            "due TEXT NOT NULL DEFAULT '', "
            "\"commit\" TEXT NOT NULL DEFAULT '', "
            "trailer TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, "
            "closed_at TEXT NOT NULL DEFAULT '', deleted INTEGER NOT NULL DEFAULT 0, "
            "section TEXT NOT NULL, task TEXT NOT NULL DEFAULT '', "
            "uuid TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY (record_id, no))")  # section: which checklist (## section)
        # C1: item identity for the event log. NO unique index — a move turns
        # the old number into a tombstone that may briefly share a uuid with the
        # renumbered live row (the replayer marks tombstones with _uuid=None).
        # The PRIMARY KEY stays (record_id, no) so `no` is the display number.
        # Pre-C1 databases: add the column + backfill existing rows (every live
        # item and tombstone gets a fresh uuid on first read).
        if "uuid" not in self.table_columns("items"):
            self.conn.execute("ALTER TABLE items ADD COLUMN uuid TEXT NOT NULL DEFAULT ''")
            self.conn.commit()  # DDL is not transactional in SQLite
            for r in self.conn.execute(
                    "SELECT record_id, no FROM items WHERE uuid=''",):
                self.conn.execute(
                    "UPDATE items SET uuid=? WHERE record_id=? AND no=?",
                    (uuid_mod.uuid4().hex, r[0], r[1]))
        # WIP.md (plans §5): the hand/registered "in progress" list — any record
        # type may sit in it (manual add or an auto switch into wip.enter)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS wip_entries "
            "(record_id TEXT PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE, "
            "pos INTEGER NOT NULL, added_at TEXT NOT NULL, "
            "source TEXT NOT NULL)")  # source: 'manual' | 'auto'
    # ---- template-driven schema -------------------------------------------

    def ensure_type(self, spec: RecordTypeSpec) -> None:
        """No-op: records/record_changelog are single shared tables (created in
        __init__); the spec only names the `type` value it stores."""

    def table_columns(self, record_type: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({record_type})")}

    def template_matches_table(self, spec: RecordTypeSpec) -> bool:
        cols = self.table_columns("records")
        base = {"id", "type", "title", "status", "summary",
                "commit", "created", "sections"}
        return base <= cols

    # records-table columns a `set` event may carry (C2.2); the replayer
    # ignores the rest — sections ride as section.set events
    _SET_FIELDS = ("title", "summary", "status", "commit")

    # ---- records (single table, type column) --------------------------------

    # ---- X0: change tracking + the (C2) event hook -------------------------

    def _rec_uuid(self, record_id: str) -> str:
        """C2: the record's stored uuid (events reference uuids, not ids);
        '' when the record is unknown (the replayer skips dangling refs)."""
        row = self.conn.execute("SELECT uuid FROM records WHERE id=?",
                               (record_id,)).fetchone()
        return row["uuid"] if row else ""

    def _item_uuid(self, record_id: str, no: str) -> str:
        row = self.conn.execute(
            "SELECT uuid FROM items WHERE record_id=? AND no=?",
            (record_id, no)).fetchone()
        return row["uuid"] if row else ""

    def _task_uuid(self, task_id: str) -> str:
        """The task's uuid for an items.task ref (C2.2: events use uuid)."""
        return self._rec_uuid(task_id) if task_id else ""

    def _touch(self, record_id: str) -> None:
        self.touched.add(record_id)

    def _touch_view(self, view: str) -> None:
        self.touched_views.add(view)

    def _emit(self, op: str, record_id: str | None = None, **payload) -> None:
        """C2: the event hook — accumulate one event in self._events (flushed
        to .proj/events.jsonl before the DB commit, C2.3). Every write method
        calls it where it marks the record touched; the op + payload follow the
        C2.2 table (the replayer in replay.py knows the same set). `touched` /
        `touched-view` are internal X0 change tracking, NOT events (skipped)."""
        if op in ("touched", "touched-view") or not self.emitting:
            return  # F6 tracking / replay-driven — not part of the event log
        self._seq += 1
        self._events.append(ev.make(op, actor=reporter(), at=self._hlc.tick(),
                                    seq=self._seq,
                                    rec=self._rec_uuid(record_id) if record_id else "",
                                    **payload))

    def meta_delete(self, key: str) -> None:
        """Delete one meta key (C4: `proj sync` consumes ev:notifications)."""
        self.conn.execute("DELETE FROM meta WHERE key=?", (key,))
        self.conn.commit()

    def _flush_events(self) -> None:
        """C2.3: before the DB commit, append every accumulated event with ONE
        os.write (O_APPEND — a crash never interleaves lines, the file always
        ends with `\n` so a union merge never fuses two lines), then update the
        incremental-replay bookkeeping in meta (ev:size / ev:prefix_hash /
        ev:last_key / ev:last_at, C3.2) and drop the buffer. A rollback discards
        the events (they were never written)."""
        if not self._events or self._events_flushed:
            return
        ev.append(self.path.parent / "events.jsonl", self._events)
        # C3.2: the local replay bookkeeping — the events this transaction just
        # wrote are now "seen", so the next command's incremental replay skips
        # them (size/prefix-hash of the whole file, the max sort key, HLC last)
        import hashlib
        p = self.path.parent / "events.jsonl"
        raw = p.read_bytes()
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("ev:size", str(len(raw))))
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("ev:prefix_hash", hashlib.sha1(raw).hexdigest()))
        key = max(ev.sort_key(e) for e in self._events)
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("ev:last_key", json.dumps(list(key))))
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("ev:last_at", str(self._hlc._last)))
        self._events = []
        self._events_flushed = True

    # ---- F1/F2: md file fingerprints (local cache, not project data) -------
    # Spec F1.1: written through the plain connection but deliberately
    # OUTSIDE the X0 write methods — no `touched`, no `_emit`: the fingerprint
    # is pure cache (never an event, never --json, never the lock); git
    # collab (C4) keeps it local to the cache.

    def set_fingerprint(self, record_id: str, mtime_ns: int | None,
                        size: int | None, h: str) -> None:
        """F1: remember a record md's (mtime_ns, size, content sha1) after a
        write or an ingest (a pure cache write: no `touched`, no `_emit`)."""
        self.conn.execute(
            "UPDATE records SET md_mtime_ns=?, md_size=?, md_hash=? WHERE id=?",
            (mtime_ns, size, h, record_id))

    def fingerprints_all(self, spec: RecordTypeSpec) -> dict[str, tuple[int, int, str]]:
        """F1: every stored md fingerprint of a type in ONE query
        (record_id -> (mtime_ns, size, sha1)); rows never written (NULL hash)
        are absent — callers treat a missing key as "no fingerprint yet"."""
        return {r["id"]: (r["md_mtime_ns"], r["md_size"], r["md_hash"])
                for r in self.conn.execute(
                    "SELECT id, md_mtime_ns, md_size, md_hash FROM records "
                    "WHERE type=? AND md_hash IS NOT NULL", (spec.record_type,))}

    def fingerprint_of(self, record_id: str) -> tuple[int, int, str] | None:
        """F1: a record md's stored fingerprint (mtime_ns, size, sha1), or None
        when never written (fresh init / pre-F1 rows are NULL)."""
        row = self.conn.execute(
            "SELECT md_mtime_ns, md_size, md_hash FROM records WHERE id=?",
            (record_id,)).fetchone()
        if row is None or row["md_hash"] is None:
            return None
        return (row["md_mtime_ns"], row["md_size"], row["md_hash"])

    def view_fp(self, name: str) -> tuple[int, int, str] | None:
        """F1: a rendered view file's fingerprint (WIP.md / CHANGELOG.md) — the
        views have no records row, so theirs live in `meta` under `fp:<name>`
        as "mtime_ns,size,hash"."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"fp:{name}",)).fetchone()
        if row is None:
            return None
        mtime_ns, size, h = row["value"].split(",")
        return (int(mtime_ns), int(size), h)

    def set_view_fp(self, name: str, mtime_ns: int, size: int, h: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"fp:{name}", f"{mtime_ns},{size},{h}"))

    # ---- record rows -------------------------------------------------------

    def insert(self, spec: RecordTypeSpec, id_: str, title: str,
               status: str, created: str, summary: str = "",
               commit: str = "", uuid: str | None = None) -> str:
        """C1: a record's uuid (event-log identity) is stored in the `uuid`
        column — generated here when the caller does not supply one (the
        replayer supplies it on replay). Returns the stored uuid so a caller
        can reference the record in its events."""
        if uuid is None:
            uuid = uuid_mod.uuid4().hex
        self.conn.execute(
            "INSERT INTO records (id, type, title, status, summary, \"commit\", "
            "created, sections, uuid) VALUES (?,?,?,?,?,?,?,?,?)",
            (id_, spec.record_type, title, status, summary, commit, created,
             "{}", uuid))
        self._touch(id_)
        self._emit("create", id_, id=id_, type=spec.record_type, title=title,
                   status=status, uuid=uuid, created=created,
                   summary=summary, commit=commit)
        return uuid

    def get(self, spec: RecordTypeSpec, id_: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM records WHERE type=? AND id=?",
            (spec.record_type, id_)).fetchone()
        return dict(row) if row else None

    def all(self, spec: RecordTypeSpec) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM records WHERE type=? ORDER BY id", (spec.record_type,))]

    def set(self, spec: RecordTypeSpec, id_: str, **cols: str) -> None:
        """Update only the given columns; C2 records the DIFF — a `set` event
        (only the changed fields) for the record-table columns, and one
        `section.set` per changed section when `sections` changed (C2.2: the
        replayer overwrites field-by-field, so unchanged columns emit nothing)."""
        old_row = self.get(spec, id_)
        old = {k: old_row[k] for k in old_row} if old_row else {}
        for k, v in cols.items():
            if k != "sections" and k in self._SET_FIELDS and old.get(k) != v:
                self._emit("set", id_, fields={k: v})
        if "sections" in cols and old.get("sections") != cols["sections"]:
            new_sec = json.loads(cols["sections"] or "{}")
            old_sec = json.loads(old.get("sections") or "{}")
            for name in sorted(set(new_sec) | set(old_sec)):
                if new_sec.get(name) != old_sec.get(name):
                    self._emit("section.set", id_, name=name,
                               text=new_sec.get(name, ""))
        assignments = ", ".join(f'"{k}" = ?' for k in cols)
        self.conn.execute(
            f"UPDATE records SET {assignments} WHERE type=? AND id=?",
            (*cols.values(), spec.record_type, id_))
        self._touch(id_)

    def delete(self, spec: RecordTypeSpec, id_: str) -> None:
        self.conn.execute("DELETE FROM record_changelog WHERE record_id = ?", (id_,))
        # relations edges cascade with the record (D8 FK), no manual cleanup
        self.conn.execute("DELETE FROM records WHERE type=? AND id=?",
                          (spec.record_type, id_))
        self._touch(id_)
        self._emit("delete", id_)

    def max_seq(self, spec: RecordTypeSpec) -> int:
        """The largest record number carried by this type's ids (digits only)."""
        rows = self.conn.execute(
            "SELECT id FROM records WHERE type=?", (spec.record_type,)).fetchall()
        best = 0
        for (s,) in rows:
            tail = s[len(spec.code) + 1:] if spec.code and s.startswith(spec.code + "-") else s
            if tail.isdigit():
                best = max(best, int(tail))
        return best

    # ---- record changelog (single table; one status column per row) ----

    def add_log(self, record_id: str, message: str = "",
                category: str = "Changed", when: str | None = None) -> None:
        # the row stores the record's status AFTER this event: callers set the
        # status first (a change) or leave it untouched (any other event)
        row = self.conn.execute("SELECT status FROM records WHERE id=?",
                                (record_id,)).fetchone()
        # the row stores the record's status AFTER this event
        status = row[0] if row else ""
        # C2: the event's date must EQUAL the stored date (one utc_now() for
        # both — two calls would differ by microseconds and replay would diverge)
        date = when or utc_now()
        self.conn.execute(
            "INSERT INTO record_changelog (record_id, date, category, message, "
            "status, actor) VALUES (?,?,?,?,?,?)",
            (record_id, date, category, message, status, reporter()))
        self._touch(record_id)
        self._emit("log", record_id, date=date, category=category,
                   message=message, status=status)

    def logs(self, record_id: str, limit: int | None = None) -> list[dict]:
        q = ("SELECT date, category, message, status, actor "
             "FROM record_changelog WHERE record_id=? ORDER BY date DESC, id DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(q, (record_id,))]

    def first_date_to(self, record_id: str, to_status: str) -> str:
        row = self.conn.execute(
            "SELECT date FROM record_changelog WHERE record_id=? AND status=? "
            "ORDER BY date, id", (record_id, to_status)).fetchone()
        return row[0] if row else ""

    # ---- global changelog ----------------------------------------------------

    def add_cl_entry(self, version: str, category: str, message: str,
                     when: str | None = None) -> None:
        date = when or utc_now()  # C2: stored and event date are the same value
        self.conn.execute(
            "INSERT INTO changelog_entries (version, category, message, date) "
            "VALUES (?, ?, ?, ?)",
            (version, category, message, date))
        self._touch_view("changelog")
        # C2: the global changelog is project-level (rec="") — the replayer
        # stamps Unreleased placeholders onto a released version (C3.1)
        self._emit("changelog.add", None, version=version, category=category,
                   message=message, date=date)

    def release_cl(self, version: str, unreleased: str) -> None:
        """X0: move every Unreleased CHANGELOG entry onto a released version
        (was a bare UPDATE in views.release); touches the changelog view."""
        self.conn.execute(
            "UPDATE changelog_entries SET version = ? WHERE version = ?",
            (version, unreleased))
        self._touch_view("changelog")
        self._emit("changelog.release", None, version=version)

    def cl_entries(self, version: str | None = None) -> list[dict]:
        if version:
            rows = self.conn.execute(
                "SELECT * FROM changelog_entries WHERE version = ? ORDER BY id",
                (version,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM changelog_entries ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---- relations (supersede + related + parent) --------------------------

    def add_relation(self, source: str, target: str, kind: str) -> None:
        """One edge, idempotent per (source, target, kind): INSERT OR IGNORE —
        re-adding an existing edge is a no-op, other kinds are never clobbered.
        kind "supersede": same-type, source supersedes target; "related": any
        type, unordered; "parent": source=parent, target=child task (at most
        one parent per task: the relations_one_parent unique index). FK: both
        ids must be existing records (D8) — write-side guards keep dangling
        refs out at the CLI/MCP surface."""
        self.conn.execute(
            "INSERT OR IGNORE INTO relations (source, target, kind) VALUES (?,?,?)",
            (source, target, kind))
        self._touch(source)
        self._touch(target)
        # C2: events reference uuids — the target's uuid (replayer resolves it)
        self._emit("rel.add", source, kind=kind, target=self._rec_uuid(target))

    def remove_relation(self, source: str, target: str, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM relations WHERE source=? AND target=? AND kind=?",
            (source, target, kind))
        self._touch(source)
        self._touch(target)
        self._emit("rel.del", source, kind=kind, target=self._rec_uuid(target))

    def set_parent(self, child_id: str, parent_id: str | None) -> None:
        """Re-home a task: drop its old parent edge (if any), write the new one
        (None = unparented). One transaction; the unique index forbids a
        second parent edge in between. Touches the child and the old AND the
        new parent (X0)."""
        old = [r[0] for r in self.conn.execute(
            "SELECT source FROM relations WHERE target=? AND kind='parent'",
            (child_id,))]
        self.conn.execute(
            "DELETE FROM relations WHERE target=? AND kind='parent'", (child_id,))
        if parent_id is not None:
            self.conn.execute(
                "INSERT INTO relations (source, target, kind) VALUES (?,?,?)",
                (parent_id, child_id, "parent"))
        self._touch(child_id)
        for old_parent in old:
            self._touch(old_parent)
            # C2.2: a re-home is the old parent edge dropped (rel.del) + the new
            # one written (rel.add) — both reference uuids
            self._emit("rel.del", child_id, kind="parent",
                       target=self._rec_uuid(old_parent))
        if parent_id is not None:
            self._touch(parent_id)
            self._emit("rel.add", child_id, kind="parent",
                       target=self._rec_uuid(parent_id))

    def supersede_of(self, source_id: str) -> list[dict]:
        """Targets this record supersedes (kind=supersede, source=row), id order."""
        return [dict(r) for r in self.conn.execute(
            "SELECT target FROM relations "
            "WHERE source=? AND kind='supersede' ORDER BY rowid", (source_id,))]

    def superseded_by_of(self, target_id: str) -> list[dict]:
        """Records that supersede this one (kind=supersede, target=row), id order."""
        return [dict(r) for r in self.conn.execute(
            "SELECT source FROM relations "
            "WHERE target=? AND kind='supersede' ORDER BY rowid", (target_id,))]

    def related_of(self, record_id: str) -> list[str]:
        """Other records sharing a related edge (either direction). Unordered."""
        out: list[str] = []
        for r in self.conn.execute(
                "SELECT target AS other FROM relations "
                "WHERE source=? AND kind='related'", (record_id,)):
            out.append(r["other"])
        for r in self.conn.execute(
                "SELECT source AS other FROM relations "
                "WHERE target=? AND kind='related'", (record_id,)):
            if r["other"] not in out:
                out.append(r["other"])
        return out

    def children_of(self, parent_id: str) -> list[dict]:
        """Direct children this record parents (kind=parent, source=row)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT target FROM relations "
            "WHERE source=? AND kind='parent' ORDER BY target", (parent_id,))]

    def parent_of(self, child_id: str) -> dict | None:
        """This task's parent (kind=parent, target=row): {source} or None."""
        row = self.conn.execute(
            "SELECT source FROM relations "
            "WHERE target=? AND kind='parent' LIMIT 1", (child_id,)).fetchone()
        return dict(row) if row else None

    def picked_by(self, task_id: str) -> list[str]:
        """The records whose LIVE checklist items reference this task
        (K4 `picked by`: items.task, deduped record-wide). Unordered."""
        out: list[str] = []
        for r in self.conn.execute(
                "SELECT DISTINCT record_id FROM items "
                "WHERE task=? AND deleted=0 AND task!=''", (task_id,)):
            if r["record_id"] not in out:
                out.append(r["record_id"])
        return out

    def tasks_referenced_by(self, record_id: str) -> list[str]:
        """The distinct task ids referenced by this record's LIVE checklist items
        (K4 `picked by`, deduped record-wide) — the reverse of `picked_by`."""
        out: list[str] = []
        for r in self.conn.execute(
                "SELECT DISTINCT task FROM items "
                "WHERE record_id=? AND deleted=0 AND task!=''", (record_id,)):
            if r["task"] not in out:
                out.append(r["task"])
        return out

    def descendants_of(self, record_id: str) -> set[str]:
        """Every record id hanging below record_id via parent edges (any depth),
        cycle-safe: a record already on the walk is never re-queued."""
        out: set[str] = set()
        seen: set[str] = {record_id}
        stack = [record_id]
        while stack:
            cur = stack.pop()
            for c in self.children_of(cur):
                cid = c["target"]
                if cid in seen:
                    continue
                seen.add(cid)
                out.add(cid)
                stack.append(cid)
        return out

    # ---- item checklists (items.py) -----------------------------------------

    def items_load(self, record_id: str) -> list[dict]:
        """All rows of the record's checklist (tombstones included), in display
        order (pos). Plain dicts."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM items WHERE record_id=? ORDER BY pos", (record_id,))]

    def items_save(self, record_id: str, rows: list[dict]) -> None:
        """One transaction: delete and re-insert all rows of the record
        (pos = the list index). The whole checklist is rewritten as a unit;
        items.py is the only writer.
        C1: the `uuid` of every row is preserved across the delete/re-insert —
        a row carrying a uuid keeps it; a fresh row (no uuid) gets one here,
        so the item identity is stable for the event log (C2 diffs by uuid)."""
        # C2: capture the old rows by uuid (before the delete) so the diff
        # below emits item.set for field changes and item.add for new rows
        # (the ops the replayer knows); replay-driven saves (emitting=False)
        # skip the diff entirely
        old_by_uuid = {r["uuid"]: dict(r) for r in
                       self.conn.execute("SELECT * FROM items WHERE record_id=?",
                                         (record_id,))}
        existing = {r["no"]: r["uuid"] for r in old_by_uuid.values()}
        self.conn.execute("DELETE FROM items WHERE record_id=?", (record_id,))
        new_by_uuid = {}
        new_pos: dict[str, int] = {}
        for pos, it in enumerate(rows):
            # the uuid rides under `uuid` (the items.py write path, via Item.to_row)
            # or `_uuid` (the replayer's temporary carrier, C1); a row in neither
            # keeps its stored uuid (matched by `no`) or gets a fresh one
            it_uuid = it.get("uuid") or it.get("_uuid") or existing.get(it["no"], "")
            if not it_uuid:
                it_uuid = uuid_mod.uuid4().hex
            self.conn.execute(
                "INSERT INTO items (record_id, no, pos, text, state, note, owner, "
                "due, \"commit\", trailer, created, closed_at, deleted, section, "
                "task, uuid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, it["no"], pos, it["text"], it.get("state", ""),
                 it.get("note", ""), it.get("owner", ""), it.get("due", ""),
                 it.get("commit", ""), it.get("trailer", ""),
                 it.get("created") or utc_now(), it.get("closed_at", ""),
                 1 if it.get("deleted") else 0,
                 it.get("section", ""), it.get("task", ""), it_uuid))
            new_by_uuid[it_uuid] = it
            # R3/C2: the row's `pos` (the index in THIS save's list) rides on
            # the item.add event, exactly like item.move's `pos` — the replay
            # lands a fresh row where the write path put it, so the rebuilt
            # list order equals the live one (C6 compares `pos`)
            if it_uuid not in old_by_uuid:
                new_pos[it_uuid] = pos
        self._touch(record_id)
        if self.emitting:
            self._emit_items_diff(record_id, old_by_uuid, new_by_uuid, new_pos)

    _ITEM_EVENT_FIELDS = ("text", "state", "note", "owner", "due", "task")

    def _emit_items_diff(self, record_id: str, old_by_uuid: dict,
                         new_by_uuid: dict, new_pos: dict | None = None) -> None:
        """C2: the item events that transform the old checklist into the new one
        (the replayer knows item.add / item.set, both resolved by the row's uuid).
        A new uuid → item.add carrying its `pos` in THIS save's list (R3/C2:
        the replayer inserts exactly there — like item.move's `pos` — so the
        replayed order equals the write path's; pre-pos events keep the
        legacy tail/after-`after` insert); a uuid present in both with
        changed fields → item.set. Replay keeps the existing rows' order, so
        no reordering op is needed for the edit/check/set flow."""
        def _task(uu):
            # an item's task ref rides as the target's uuid (C2.2), not its id
            return self._rec_uuid(uu) if uu else ""
        for uu, it in new_by_uuid.items():
            no = it["no"]
            common = {"no": no, "text": it.get("text", ""),
                      "section": it.get("section", ""),
                      "task": _task(it.get("task", ""))}
            if uu not in old_by_uuid:
                pos = (new_pos or {}).get(uu)
                self._emit("item.add", record_id, item=uu,
                           state=it.get("state", ""), note=it.get("note", ""),
                           owner=it.get("owner", ""), due=it.get("due", ""),
                           commit=it.get("commit", ""), trailer=it.get("trailer", ""),
                           created=it.get("created") or utc_now(),
                           closed_at=it.get("closed_at", ""),
                           pos=pos, **common)
                continue
            old = old_by_uuid[uu]
            fields = {f: it.get(f, "") for f in self._ITEM_EVENT_FIELDS
                      if old.get(f, "") != it.get(f, "")}
            if (1 if old.get("deleted") else 0) != (1 if it.get("deleted") else 0):
                fields["deleted"] = 1 if it.get("deleted") else 0
            if not fields:
                continue
            if it.get("state") in ("done", "failed", "dropped") and not old.get("closed_at"):
                self._emit("item.set", record_id, item=uu, fields=fields,
                           date=it.get("closed_at") or utc_now())
            else:
                self._emit("item.set", record_id, item=uu, fields=fields)

    def item_set_state(self, record_id: str, no: str, state: str,
                       closed_at: str) -> None:
        """X0: flip one checklist item's state (+closed_at) — the single entry
        for the P3 forward sync (was a bare UPDATE in items.sync_task_ref);
        runs inside the caller's transaction. `closed_at` is decided by the
        caller (the CLOSED_STATES list lives in items.py, not here)."""
        self.conn.execute(
            "UPDATE items SET state=?, closed_at=? WHERE record_id=? AND no=?",
            (state, closed_at, record_id, no))
        self._touch(record_id)
        self._emit("item.set", record_id, item=self._item_uuid(record_id, no),
                   fields={"state": state})

    # ---- WIP.md (plans §5): the registered "in progress" list (wip_entries) ----

    def wip_all(self) -> list[dict]:
        """Every WIP entry, in list order (pos): record_id, pos, added_at, source."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM wip_entries ORDER BY pos, record_id")]

    def wip_has(self, record_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM wip_entries WHERE record_id=?", (record_id,)).fetchone() is not None

    def wip_add(self, record_id: str, source: str) -> int:
        """Register a record in WIP.md at the end of the list (its new pos)."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(pos), -1) FROM wip_entries").fetchone()
        pos = row[0] + 1
        self.conn.execute(
            "INSERT INTO wip_entries (record_id, pos, added_at, source) VALUES (?,?,?,?)",
            (record_id, pos, utc_now(), source))
        self._touch(record_id)
        self._touch_view("wip")
        self._emit("wip.add", record_id, added_at=utc_now(), source=source)
        return pos

    def wip_remove(self, record_id: str) -> None:
        """Un-register a record; remaining entries keep their pos (gaps allowed)."""
        self.conn.execute("DELETE FROM wip_entries WHERE record_id=?", (record_id,))
        self._touch(record_id)
        self._touch_view("wip")
        self._emit("wip.del", record_id)

    def rollback(self) -> None:
        """Roll the open write transaction back and clear X0 change tracking
        (a failed command leaves neither half-written rows nor a stale `touched`
        set — the next command starts clean)."""
        self.conn.rollback()
        self.touched.clear()
        self.touched_views.clear()
        self._events = []  # C2: a rolled-back command writes no events
        self._events_flushed = True
        self._rolled_back = True

    def begin(self) -> None:
        # R3: open the write transaction (deferred — the exclusive lock is taken
        # lazily on the FIRST write, so a pure read through a `with` takes no
        # lock at all, and an inner READ-ONLY connection of the same command
        # (e.g. _check_parent_ref inside task.move) can close before we commit).
        # C5: refuse to open a write transaction while a git merge / rebase is
        # in flight — the single choke point for every write command (CLI + MCP
        # all funnel through begin); read-only opens (pure `ls`/`show`) are
        # unaffected.
        from proj.core.config import ProjConfig
        from proj.hooks import git_in_progress  # lazy: hooks imports config
        # the DB lives at <root>/.proj/cache.sqlite — the project root is two
        # levels up
        reason = git_in_progress(ProjConfig(root=self.path.parent.parent))
        if reason is not None:
            raise ValueError(f"git: {reason}")
        self.conn.execute("BEGIN")

    def __enter__(self) -> Db:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """One command = one transaction: commit on success, roll back on error
        (a mid-command exception leaves no half-written rows, R3), close either
        way (A4 — no leaked connection holds the write lock)."""
        try:
            if exc_type is None:
                self._flush_events()  # C2.3: log BEFORE the commit
                self.conn.commit()
            else:
                self.rollback()
        except sqlite3.Error:
            # a BEGIN-less (read-only) `with` has nothing to end — close goes on
            pass
        finally:
            self.conn.close()

    def close(self) -> None:
        """The non-context exit point (item ops hold the db via open_db +
        close, not `with`): commit + flush the accumulated events BEFORE the
        connection closes (a `with` command already did both in __exit__ —
        the _events_flushed flag keeps this idempotent)."""
        if not self._events_flushed and not self._rolled_back:
            try:
                self.conn.commit()
                self._flush_events()
            except sqlite3.Error:
                pass  # a BEGIN-less (read-only) close has nothing to end
        self.conn.close()


def next_id(spec: RecordTypeSpec, db: Db) -> str:
    """The next record id: {code}-{digits}, fixed width `spec.numbered` (W=4 →
    DEC-0001 … DEC-9999, task W=6 → T-000001 … T-999999). The record id has
    NO letter overflow: once the all-digit space is used up, new ids raise —
    a single project will not run out (9,999 / 999,999 records)."""
    n = db.max_seq(spec) + 1
    w = int(spec.numbered)
    if n > 10 ** w:
        raise ValueError(
            f"record ids of type {spec.code!r} are exhausted "
            f"(all-digit {w}-digit suffix space used up; max "
            f"{spec.code}-{10 ** w - 1:0{w}d})")
    return f"{spec.code}-{n:0{w}d}"


def parse_summary(arg: str, spec: ChangelogSpec | None = None) -> tuple[str, str]:
    """'Changed: text' → ('Changed', 'text'); plain text → (spec.default_category, text).

    The category whitelist comes from profiles/changelog.yaml (spec), not a
    hard-coded tuple. spec=None loads the default spec (for direct/test calls)."""
    if spec is None:
        from proj.core.templates import load_changelog_spec
        spec = load_changelog_spec()
    m = CATEGORY_RE.match(arg.strip())
    if m:
        raw = m.group(1).strip()
        canonical = next((c for c in spec.categories if c.lower() == raw.lower()), None)
        if canonical is None:
            raise ValueError(f"unknown changelog category {raw!r} "
                             f"(expected one of: {', '.join(spec.categories)})")
        return canonical, m.group(2).strip()
    return spec.default_category, arg.strip()


# ---- lock file -------------------------------------------------------------

def write_lock(root: Path, specs: dict[str, RecordTypeSpec], profiles: list[str]) -> Path:
    path = root / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)  # S.10: the .proj/ dir
    data = {
        "schema_version": SCHEMA_VERSION,
        "profiles": profiles,
        "record_types": {s.record_type: {
            "dir": s.dir, "numbered": s.numbered,
            "status_allowed": s.status_allowed,
            "status_emoji": s.status_emoji,
            "verbs": {v: {"from": f, "to": t} for v, (f, t) in s.verbs.items()},
            "sections": [asdict(x) for x in s.sections],
            "code": s.code,
            "status_done": list(s.status_done),
            "status_terminal": list(s.status_terminal),
            "gates": {"tasks": list(s.gates_tasks)},
            "items": [asdict(x) for x in s.items],
            "wip": {"enter": list(s.wip_enter)},
        } for s in specs.values()},
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path


def read_lock(root: Path) -> dict | None:
    path = root / LOCK_FILE
    if not path.is_file():
        return None
    from proj.core.templates import _yaml_load
    return _yaml_load(path.read_text(encoding="utf-8"))


def specs_from_lock(lock: dict) -> dict[str, RecordTypeSpec]:
    from proj.core.templates import TemplateSection

    out: dict[str, RecordTypeSpec] = {}
    for record_type, d in lock.get("record_types", {}).items():
        out[record_type] = RecordTypeSpec(
            record_type=record_type,
            dir=d["dir"],
            numbered=d.get("numbered", 4),
            status_allowed=list(d.get("status_allowed", [])),
            status_emoji=dict(d.get("status_emoji", {})),
            verbs={v: (x["from"], x["to"]) for v, x in d.get("verbs", {}).items()},
            sections=[TemplateSection(**s) for s in d.get("sections", [])],
            code=str(d.get("code", "")),
            status_done=list(d.get("status_done", [])),
            # terminal/gates: the lock may predate them (old projects) — the
            # terminal set falls back to the done set (the pre-gate behavior)
            status_terminal=list(d.get("status_terminal", [])) or list(d.get("status_done", [])),
            gates_tasks=list(d.get("gates", {}).get("tasks", [])),
            items=_items_from_lock(d.get("items")),
            # WIP: the lock may predate it (old projects) - manual-only default
            wip_enter=list(d.get("wip", {}).get("enter", [])),
        )
    return out


def _items_from_lock(d) -> list[ItemsSpec]:
    """The `items:` checklists back from the lock ([] when the lock predates
    items, or before P1 — a pre-P1 lock carries ONE object: promote it to a
    one-element list; `states` is ignored: item states are global)."""
    if not d:
        return []
    if isinstance(d, dict):
        d = [d]
    return [ItemsSpec(
        section=x["section"],
        noun=x.get("noun", "item"),
        fields=list(x.get("fields", [])),
        promote_from=list(x.get("promote_from", [])),
        promote_to=x.get("promote_to", ""),
        wip=bool(x.get("wip", False)),
        gate=list(x.get("gate", [])),
    ) for x in d]
