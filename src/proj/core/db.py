"""SQLite core (.proj.sqlite) — the single source of truth (v3).

Unified tables (schema v3, no migration — dev phase, rebuild .proj.sqlite):

    records(id PK, type, title, status, one_sentence, commit, created, sections JSON)
    record_changelog(id PK, record_id, date, category, comment, status)

record_changelog: every row stores the record's status AFTER that event
(`status` = the new state on a status change, the unchanged state on any
other event), so a changelog line renders as `- `date - emoji status``.

Plus the global changelog: changelog_entries(id, version, category, text, date).

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

from proj.core.templates import RecordTypeSpec

SCHEMA_VERSION = 3
LOCK_FILE = ".proj.lock"

CATEGORY_RE = re.compile(r"^([A-Za-z][A-Za-z ]*):\s*(.+)$")


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
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS changelog_entries "
            "(id INTEGER PRIMARY KEY, version TEXT, category TEXT, text TEXT, date TEXT)")
        # inter-record relations: one row per edge. kind "supersede": same-type,
        # ordered (pos); "related": any type, unordered; "parent": source=parent,
        # target=child task (any parent type), single parent enforced in the app.
        # D8: no type columns — the record type resolves from the id prefix
        # (registry code→type); FK CASCADE deletes edges with their record.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS relations "
            "(id INTEGER PRIMARY KEY, source TEXT NOT NULL "
            "REFERENCES records(id) ON DELETE CASCADE, "
            "target TEXT NOT NULL "
            "REFERENCES records(id) ON DELETE CASCADE, "
            "kind TEXT NOT NULL, "
            "pos INTEGER NOT NULL DEFAULT 0, UNIQUE(source, target, kind))")
        # unified record storage: one table per type was replaced by a type column
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS records "
            "(id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, status TEXT, "
            "one_sentence TEXT, \"commit\" TEXT, created TEXT, sections TEXT)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS record_changelog "
            "(id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, date TEXT, "
            "category TEXT, comment TEXT, status TEXT)")

    # ---- template-driven schema -------------------------------------------

    def ensure_kind(self, spec: RecordTypeSpec) -> None:
        """No-op in schema v2: records/record_changelog are single shared tables
        (created in __init__); the spec only names the `type` value it stores."""

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

    def max_int_id(self, spec: RecordTypeSpec) -> int:
        row = self.conn.execute(
            "SELECT id FROM records WHERE type=?", (spec.record_type,)).fetchall()
        best = 0
        for r in row:
            s = r[0]
            tail = (s[len(spec.code) + 1:] if spec.code
                    and s.startswith(spec.code + "-") else s)
            if tail.isdigit():
                best = max(best, int(tail))
        return best

    # ---- record changelog (single table; one status column per row) ----

    def add_log(self, record_id: str, comment: str = "",
                category: str = "Changed", when: str | None = None) -> None:
        # the row stores the record's status AFTER this event: callers set the
        # status first (a change) or leave it untouched (any other event)
        row = self.conn.execute("SELECT status FROM records WHERE id=?",
                                (record_id,)).fetchone()
        status = row[0] if row else ""
        self.conn.execute(
            "INSERT INTO record_changelog (record_id, date, category, comment, "
            "status) VALUES (?,?,?,?,?)",
            (record_id, when or utc_now(), category, comment, status))
        self.conn.commit()

    def logs(self, record_id: str, limit: int | None = None) -> list[dict]:
        q = ("SELECT date, category, comment, status "
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

    def add_cl_entry(self, version: str, category: str, text: str,
                     when: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO changelog_entries (version, category, text, date) "
            "VALUES (?, ?, ?, ?)",
            (version, category, text, when or utc_now()))
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

    # ---- relations (supersede + related) -----------------------------------

    def add_relation(self, source: str, target: str, kind: str, pos: int = 0) -> None:
        """One edge. kind "supersede": same-type, ordered by pos, source supersedes target.
        kind "related": any type, unordered (pos ignored). kind "parent":
        source=parent, target=child task. FK: both ids must be existing records
        (D8) — write-side guards keep dangling refs out at the CLI/MCP surface."""
        self.conn.execute(
            "INSERT OR REPLACE INTO relations (source, target, kind, pos) "
            "VALUES (?,?,?,?)",
            (source, target, kind, pos))
        self.conn.commit()

    def remove_relation(self, source: str, target: str, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM relations WHERE source=? AND target=? AND kind=?",
            (source, target, kind))
        self.conn.commit()

    def supersede_of(self, source_id: str) -> list[dict]:
        """Targets this record supersedes (kind=supersede, source=row), in pos order."""
        return [dict(r) for r in self.conn.execute(
            "SELECT target, pos FROM relations "
            "WHERE source=? AND kind='supersede' ORDER BY pos, id", (source_id,))]

    def superseded_by_of(self, target_id: str) -> list[dict]:
        """Records that supersede this one (kind=supersede, target=row), in pos order."""
        return [dict(r) for r in self.conn.execute(
            "SELECT source, pos FROM relations "
            "WHERE target=? AND kind='supersede' ORDER BY pos, id", (target_id,))]

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

    def close(self) -> None:
        self.conn.close()


def next_id(spec: RecordTypeSpec, db: Db) -> str:
    """D9: numbered is always an int > 0 (validated in parse_template), so every
    record id is {code}-{n:0w} and the prefix always resolves back to a type."""
    n = db.max_int_id(spec) + 1
    return f"{spec.code}-{n:0{int(spec.numbered)}d}"


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
            "contain_task": s.contain_task,
            "render": {"toc": s.apply_toc},
        } for s in specs.values()},
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path


def read_lock(root: Path) -> dict | None:
    path = root / LOCK_FILE
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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
            contain_task=bool(d.get("contain_task", False)),
            apply_toc=bool(d.get("render", {}).get("toc", False)),
        )
    return out
