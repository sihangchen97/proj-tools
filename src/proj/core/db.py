"""SQLite core (.proj.sqlite) — the single source of truth.

Unified tables (schema v4):

    records(id PK, type, title, status, one_sentence, commit, created, sections JSON)
    record_changelog(id PK, record_id, date, category, message, status)
    relations(source, target, kind) — PK (source, target, kind); one-parent unique index

record_changelog: every row stores the record's status AFTER that event
(`status` = the new state on a status change, the unchanged state on any
other event), so a changelog line renders as `- `date - emoji status``;
`message` is the optional free text (--message, git-style).

Plus the global changelog:
    changelog_entries(id PK, version, category, message, date)

The lock file (.proj.lock) pins which templates built the DB so a fresh
clone without the DB can rebuild the same schema.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proj.core.templates import ChangelogSpec

import yaml

from proj.core.templates import ItemsSpec, ItemState, RecordTypeSpec

SCHEMA_VERSION = 7
LOCK_FILE = ".proj.lock"

CATEGORY_RE = re.compile(r"^([A-Za-z][A-Za-z ]*):\s*(.+)$")


class ProjError(ValueError):
    """Base for proj tool errors (A4/D4). MCP's _safe and the CLI catch it and
    return a clean 'error:' string instead of a raw traceback. ItemsError
    (engine/items.py) and friends subclass it."""


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
        # WAL + busy_timeout: several agents may share the project db; a reader
        # no longer fails with "database is locked" while another writes.
        self.conn = sqlite3.connect(path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
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
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS records "
            "(id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, status TEXT, "
            "one_sentence TEXT, \"commit\" TEXT, created TEXT, sections TEXT)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS record_changelog "
            "(id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, date TEXT, "
            "category TEXT, message TEXT, status TEXT)")
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
            "PRIMARY KEY (record_id, no))")
    # ---- template-driven schema -------------------------------------------

    def ensure_kind(self, spec: RecordTypeSpec) -> None:
        """No-op: records/record_changelog are single shared tables (created in
        __init__); the spec only names the `type` value it stores."""

    def table_columns(self, record_type: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({record_type})")}

    def template_matches_table(self, spec: RecordTypeSpec) -> bool:
        cols = self.table_columns("records")
        base = {"id", "type", "title", "status", "one_sentence",
                "commit", "created", "sections"}
        return base <= cols

    # ---- records (single table, type column) --------------------------------

    def insert(self, spec: RecordTypeSpec, id_: str, title: str,
               status: str, created: str, one_sentence: str = "",
               commit: str = "") -> None:
        self.conn.execute(
            "INSERT INTO records (id, type, title, status, one_sentence, \"commit\", "
            "created, sections) VALUES (?,?,?,?,?,?,?,?)",
            (id_, spec.record_type, title, status, one_sentence, commit, created, "{}"))
        self.conn.commit()

    def get(self, spec: RecordTypeSpec, id_: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM records WHERE type=? AND id=?",
            (spec.record_type, id_)).fetchone()
        return dict(row) if row else None

    def all(self, spec: RecordTypeSpec) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM records WHERE type=? ORDER BY id", (spec.record_type,))]

    def set(self, spec: RecordTypeSpec, id_: str, **cols: str) -> None:
        assignments = ", ".join(f'"{k}" = ?' for k in cols)
        self.conn.execute(
            f"UPDATE records SET {assignments} WHERE type=? AND id=?",
            (*cols.values(), spec.record_type, id_))
        self.conn.commit()

    def delete(self, spec: RecordTypeSpec, id_: str) -> None:
        self.conn.execute("DELETE FROM record_changelog WHERE record_id = ?", (id_,))
        # relations edges cascade with the record (D8 FK), no manual cleanup
        self.conn.execute("DELETE FROM records WHERE type=? AND id=?",
                          (spec.record_type, id_))
        self.conn.commit()

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
        status = row[0] if row else ""
        self.conn.execute(
            "INSERT INTO record_changelog (record_id, date, category, message, "
            "status) VALUES (?,?,?,?,?)",
            (record_id, when or utc_now(), category, message, status))
        self.conn.commit()

    def logs(self, record_id: str, limit: int | None = None) -> list[dict]:
        q = ("SELECT date, category, message, status "
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
        self.conn.execute(
            "INSERT INTO changelog_entries (version, category, message, date) "
            "VALUES (?, ?, ?, ?)",
            (version, category, message, when or utc_now()))
        self.conn.commit()

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
        self.conn.commit()

    def remove_relation(self, source: str, target: str, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM relations WHERE source=? AND target=? AND kind=?",
            (source, target, kind))
        self.conn.commit()

    def set_parent(self, child_id: str, parent_id: str | None) -> None:
        """Re-home a task: drop its old parent edge (if any), write the new one
        (None = unparented). One transaction; the unique index forbids a
        second parent edge in between."""
        self.conn.execute(
            "DELETE FROM relations WHERE target=? AND kind='parent'", (child_id,))
        if parent_id is not None:
            self.conn.execute(
                "INSERT INTO relations (source, target, kind) VALUES (?,?,?)",
                (parent_id, child_id, "parent"))
        self.conn.commit()

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

    def descendants_of(self, record_id: str) -> set[str]:
        """Every record id hanging below record_id via parent edges (any depth),
        cycle-safe: a record already on the walk is never re-queued.
        (views.task_ids_of is the task-rows variant — it keeps the same walk.)"""
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
        items.py is the only writer."""
        self.conn.execute("DELETE FROM items WHERE record_id=?", (record_id,))
        for pos, it in enumerate(rows):
            self.conn.execute(
                "INSERT INTO items (record_id, no, pos, text, state, note, owner, "
                "due, \"commit\", trailer, created, closed_at, deleted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, it["no"], pos, it["text"], it.get("state", ""),
                 it.get("note", ""), it.get("owner", ""), it.get("due", ""),
                 it.get("commit", ""), it.get("trailer", ""),
                 it.get("created") or utc_now(), it.get("closed_at", ""),
                 1 if it.get("deleted") else 0))
        self.conn.commit()

    def __enter__(self) -> Db:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """One command = one transaction: commit on success, roll back on error,
        close either way (A4 — no leaked connection holds the write lock)."""
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()

    def close(self) -> None:
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
            "gates": {"tasks": list(s.gates_tasks), "items": list(s.gates_items)},
            "items": (asdict(s.items) if s.items is not None else None),
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
            gates_items=list(d.get("gates", {}).get("items", [])),
            items=_items_from_lock(d.get("items")),
        )
    return out


def _items_from_lock(d) -> ItemsSpec | None:
    """The `items:` block back from the lock (None when the lock predates it)."""
    if not d:
        return None
    return ItemsSpec(
        section=d["section"],
        noun=d.get("noun", "item"),
        states=[ItemState(**s) for s in d.get("states", [])],
        fields=list(d.get("fields", [])),
        promote_from=list(d.get("promote_from", [])),
        promote_to=d.get("promote_to", ""),
    )
