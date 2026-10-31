"""SQLite core (.proj.sqlite) — the single source of truth (v3).

Per record_type two tables (created from YAML templates at init / lock replay):

    <record_type>(id PK, title, status, created, <fields...>, sections JSON)
    <record_type>_changelog(id, record_id, date, category, summary)

Plus the global changelog: changelog_entries(id, version, category, text, date).

The lock file (.proj.lock) pins which templates built the DB so a fresh
clone without the DB can rebuild the same schema.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proj.core.templates import ChangelogSpec

import yaml

from proj.core.templates import RecordTypeSpec

SCHEMA_VERSION = 1
LOCK_FILE = ".proj.lock"

CATEGORY_RE = re.compile(r"^([A-Za-z][A-Za-z ]*):\s*(.+)$")


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
        # inter-record relations (supersede + related): one row per edge.
        # supersede is ordered (pos) and same-type; related is unordered and may be cross-type.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS relations "
            "(id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_type TEXT NOT NULL, "
            "target TEXT NOT NULL, target_type TEXT NOT NULL, kind TEXT NOT NULL, "
            "pos INTEGER NOT NULL DEFAULT 0, UNIQUE(source, target, kind))")

    # ---- template-driven schema -------------------------------------------

    def ensure_kind(self, spec: RecordTypeSpec) -> None:
        # field names may collide with SQL keywords (e.g. `commit`) — quote them
        field_cols = ", ".join(f'"{f.name}" TEXT' for f in spec.fields)
        extra = f", {field_cols}" if field_cols else ""
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {spec.record_type} ("
            f"id TEXT PRIMARY KEY, title TEXT, status TEXT, "
            f"summary TEXT, created TEXT{extra}, sections TEXT)")
        for f in spec.fields:
            if f.name not in self.table_columns(spec.record_type):
                self.conn.execute(f'ALTER TABLE {spec.record_type} ADD COLUMN "{f.name}" TEXT')
                self.conn.commit()
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {spec.record_type}_changelog ("
            f"id INTEGER PRIMARY KEY, record_id TEXT REFERENCES {spec.record_type}(id), "
            f"date TEXT, category TEXT, summary TEXT)")

    def table_columns(self, record_type: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({record_type})")}

    def template_matches_table(self, spec: RecordTypeSpec) -> bool:
        cols = self.table_columns(spec.record_type)
        base = {"id", "title", "status", "summary", "created", "sections"}
        need = base | set(spec.field_names)
        return need <= cols

    # ---- records ------------------------------------------------------------

    @staticmethod
    def _q(name: str) -> str:
        """Quote a column name (field names may be SQL keywords)."""
        return f'"{name}"'

    def insert(self, spec: RecordTypeSpec, id_: str, title: str,
               status: str, created: str, fields: dict[str, str] | None = None,
               summary: str = "") -> None:
        base = ["id", "title", "status", "summary", "created", "sections"]
        names = [*base, *[self._q(f.name) for f in spec.fields]]
        vals = [id_, title, status, summary, created, "{}",
                *[(fields or {}).get(f.name, "") for f in spec.fields]]
        self.conn.execute(
            f"INSERT INTO {spec.record_type} ({', '.join(names)}) "
            f"VALUES ({', '.join('?' * len(names))})", vals)
        self.conn.commit()

    def get(self, spec: RecordTypeSpec, id_: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT * FROM {spec.record_type} WHERE id = ?", (id_,)).fetchone()
        return dict(row) if row else None

    def all(self, spec: RecordTypeSpec) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM {spec.record_type} ORDER BY id")]

    def set(self, spec: RecordTypeSpec, id_: str, **cols: str) -> None:
        assignments = ", ".join(f"{self._q(k)} = ?" for k in cols)
        self.conn.execute(
            f"UPDATE {spec.record_type} SET {assignments} WHERE id = ?",
            (*cols.values(), id_))
        self.conn.commit()

    def delete(self, spec: RecordTypeSpec, id_: str) -> None:
        table = spec.record_type
        self.conn.execute(f"DELETE FROM {table}_changelog WHERE record_id = ?", (id_,))
        self.conn.execute("DELETE FROM relations WHERE source = ?", (id_,))
        self.conn.execute("DELETE FROM relations WHERE target = ?", (id_,))
        self.conn.execute(f"DELETE FROM {spec.record_type} WHERE id = ?", (id_,))
        self.conn.commit()

    def max_int_id(self, spec: RecordTypeSpec) -> int:
        if spec.numbered == 0:
            return 0
        row = self.conn.execute(
            f"SELECT id FROM {spec.record_type}").fetchall()
        best = 0
        for r in row:
            s = r[0]
            tail = (s[len(spec.code) + 1:] if spec.code
                    and s.startswith(spec.code + "-") else s)
            if tail.isdigit():
                best = max(best, int(tail))
        return best

    # ---- per-record changelog ----------------------------------------------

    def add_log(self, record_type: str, record_id: str, summary: str,
                category: str = "Changed", when: str | None = None) -> None:
        self.conn.execute(
            f"INSERT INTO {record_type}_changelog (record_id, date, category, summary) "
            f"VALUES (?, ?, ?, ?)", (record_id, when or date.today().isoformat(),
                                     category, summary))
        self.conn.commit()

    def logs(self, record_type: str, record_id: str, limit: int | None = None) -> list[dict]:
        q = (f"SELECT date, category, summary FROM {record_type}_changelog "
             f"WHERE record_id = ? ORDER BY date DESC, id DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(q, (record_id,))]

    def first_date_to(self, record_type: str, record_id: str, to_status: str) -> str:
        row = self.conn.execute(
            f"SELECT date FROM {record_type}_changelog "
            f"WHERE record_id = ? AND summary LIKE ? ORDER BY date, id",
            (record_id, f"%→ {to_status}")).fetchone()
        return row[0] if row else ""

    # ---- global changelog ----------------------------------------------------

    def add_cl_entry(self, version: str, category: str, text: str,
                     when: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO changelog_entries (version, category, text, date) "
            "VALUES (?, ?, ?, ?)",
            (version, category, text, when or date.today().isoformat()))
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

    def add_relation(self, source: str, source_type: str, target: str,
                     target_type: str, kind: str, pos: int = 0) -> None:
        """One edge. kind "supersede": same-type, ordered by pos, source supersedes target.
        kind "related": any type, unordered (pos ignored)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO relations "
            "(source, source_type, target, target_type, kind, pos) VALUES (?,?,?,?,?,?)",
            (source, source_type, target, target_type, kind, pos))
        self.conn.commit()

    def remove_relation(self, source: str, source_type: str, target: str, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM relations WHERE source=? AND source_type=? AND target=? AND kind=?",
            (source, source_type, target, kind))
        self.conn.commit()

    def supersede_of(self, source_id: str) -> list[dict]:
        """Targets this record supersedes (kind=supersede, source=row), in pos order."""
        return [dict(r) for r in self.conn.execute(
            "SELECT target, target_type, pos FROM relations "
            "WHERE source=? AND kind='supersede' ORDER BY pos, id", (source_id,))]

    def superseded_by_of(self, target_id: str) -> list[dict]:
        """Records that supersede this one (kind=supersede, target=row), in pos order."""
        return [dict(r) for r in self.conn.execute(
            "SELECT source, source_type, pos FROM relations "
            "WHERE target=? AND kind='supersede' ORDER BY pos, id", (target_id,))]

    def related_of(self, record_id: str) -> dict[str, str]:
        """Other records sharing a related edge (either direction), {other_id: type}.
        Unordered."""
        out: dict[str, str] = {}
        for r in self.conn.execute(
                "SELECT target AS other, target_type AS otype FROM relations "
                "WHERE source=? AND kind='related'", (record_id,)):
            out[r["other"]] = r["otype"]
        for r in self.conn.execute(
                "SELECT source AS other, source_type AS otype FROM relations "
                "WHERE target=? AND kind='related'", (record_id,)):
            out.setdefault(r["other"], r["otype"])
        return out

    def close(self) -> None:
        self.conn.close()


def _letter_value(s: str) -> int:
    """A=1..Z=26, AA=27... (spreadsheet-style)."""
    v = 0
    for ch in s:
        v = v * 26 + (ord(ch) - ord("A") + 1)
    return v


def _value_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def next_id(spec: RecordTypeSpec, db: Db) -> str:
    if spec.numbered == 0:
        return ""
    n = db.max_int_id(spec) + 1
    if spec.numbered == "letter":
        return _value_letter(n)
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
            "fields": [asdict(f) for f in s.fields],
            "sections": [asdict(x) for x in s.sections],
            "code": s.code,
            "status_done": list(s.status_done),
            "features": {"supersede": s.apply_supersede, "task": s.apply_task},
            "render": {"toc": s.apply_toc, "changelog": s.apply_changelog},
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
    from proj.core.templates import TemplateField, TemplateSection

    out: dict[str, RecordTypeSpec] = {}
    for record_type, d in lock.get("record_types", {}).items():
        out[record_type] = RecordTypeSpec(
            record_type=record_type,
            dir=d["dir"],
            numbered=d.get("numbered", 4),
            status_allowed=list(d.get("status_allowed", [])),
            status_emoji=dict(d.get("status_emoji", {})),
            verbs={v: (x["from"], x["to"]) for v, x in d.get("verbs", {}).items()},
            fields=[TemplateField(**f) for f in d.get("fields", [])],
            sections=[TemplateSection(**s) for s in d.get("sections", [])],
            code=str(d.get("code", "")),
            status_done=list(d.get("status_done", [])),
            apply_supersede=bool(d.get("features", {}).get("supersede", False)),
            apply_task=bool(d.get("features", {}).get("task", False)),
            apply_toc=bool(d.get("render", {}).get("toc", False)),
            apply_changelog=bool(d.get("render", {}).get("changelog",
                                          d.get("features", {}).get("changelog", True))),
        )
    return out
