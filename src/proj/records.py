"""v3 record engine: YAML template (schema) + SQLite (storage) + md (input channel).

md file shape (rendered by the engine, no frontmatter):

    <!-- proj:decision 3f2a1b4c-... -->     <- line 1: hidden marker (kind + uuid)

    # 1. Title                              <- tool-owned heading
    Status: Accepted                        <- tool-owned managed lines
    Supersedes: 0001                        <- kind fields (non-empty only)
    Started: 2026-10-29                     <- derived from changelog

    | Date | Category | Change |          <- latest 5 changelog entries (rendered)
    |---|---|---|
    | 2026-10-29 | Changed | status Proposed → Accepted |

    ## Context                              <- user-owned content (the only
    ...                                     <- part humans/LLMs edit)

sync: md → DB (sections win) → render all md (tool-owned parts win).
"""
from __future__ import annotations

import json
import re
import uuid as _uuid
from datetime import date
from pathlib import Path

from proj import db as dbmod
from proj.config import ProjConfig
from proj.templates import KindSpec, load_registry, slugify

MARK_RE = re.compile(
    r"^<!-- proj:([\w-]+) "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) -->[ \t]*$",
    re.MULTILINE)
TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
CHECKBOX_RE = re.compile(r"^\s*[-*] \[([ xX])\]")
STATUS_LINE_RE = re.compile(r"^Status: (.+)$", re.MULTILINE)


def open_db(cfg: ProjConfig) -> dbmod.Db:
    cfg.proj_dir.mkdir(parents=True, exist_ok=True)
    return dbmod.Db(cfg.db_path)


def spec_and_db(cfg: ProjConfig, kind: str):
    reg = load_registry(cfg.root)
    if kind not in reg:
        raise ValueError(f"unknown kind {kind!r} (registered: {', '.join(sorted(reg))})")
    return reg[kind], open_db(cfg)


def by_id(cfg: ProjConfig, spec: KindSpec, db: dbmod.Db, id_: str) -> dict | None:
    row = db.get(spec, id_, by="id")
    if row is None and id_.isdigit() and isinstance(spec.numbered, int):
        row = db.get(spec, f"{int(id_):0{spec.numbered}d}", by="id")
    return row


def _display_id(spec: KindSpec, id_: str) -> str:
    return str(int(id_)) if id_.isdigit() else id_


def _id_prefix_re(spec: KindSpec) -> re.Pattern:
    if spec.numbered == "letter":
        return re.compile(r"^([A-Z]+)-")
    if spec.numbered == "date":
        return re.compile(r"^(\d{4}-\d{2}-\d{2})-")
    if isinstance(spec.numbered, int):
        return re.compile(rf"^(\d{{{spec.numbered}}})-")
    return re.compile(r"^(\S+)-")


def file_path(cfg: ProjConfig, spec: KindSpec, row: dict) -> Path:
    return cfg.root / spec.dir / (row["id"] + "-" + slugify(row["title"]) + ".md")


# ---- rendering -------------------------------------------------------------

def render_record(spec: KindSpec, row: dict, db: dbmod.Db,
                  registry: dict[str, KindSpec] | None = None) -> str:
    lines = [f"<!-- proj:{spec.kind} {row['uuid']} -->", ""]
    lines += [f"# {_display_id(spec, row['id'])}. {row['title']}", ""]
    lines.append(f"Status: {row['status']}")
    for f in spec.fields:
        v = row.get(f.name) or ""
        if v:
            lines.append(f"{f.name.capitalize()}: {v}")
    for d in spec.derived:
        v = db.first_date_to(spec.kind, row["uuid"], d.to_status)
        lines.append(f"{d.name}: {v or '—'}")
    lines.append("")
    logs = db.logs(spec.kind, row["uuid"], limit=5)
    if logs:
        lines += ["| Date | Category | Change |", "|---|---|---|"]
        lines += [f"| {e['date']} | {e['category']} | {e['summary']} |" for e in logs]
        lines.append("")
    sections = json.loads(row["sections"] or "{}")
    for sec in spec.sections:
        lines += [f"## {sec.name}", ""]
        if sec.view:
            from proj.views import render_view
            if registry is None:
                registry = load_registry(Path(db.path).parent.parent)
            lines += (render_view(sec.view, spec, row, db, registry.get("task")) or [""])
        else:
            lines.append(sections.get(sec.name.lower(), "").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_all(cfg: ProjConfig) -> list[Path]:
    """Phase 2: DB → every record file + global views. Returns written paths."""
    from proj.views import render_changelog_md, render_tasklist_md
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    written: list[Path] = []
    for spec in reg.values():
        d = cfg.root / spec.dir
        d.mkdir(parents=True, exist_ok=True)
        for row in db.all(spec):
            p = file_path(cfg, spec, row)
            p.write_text(render_record(spec, row, db, reg), encoding="utf-8")
            written.append(p)
    for view in (render_tasklist_md(cfg, db, reg), render_changelog_md(cfg, db)):
        if view is not None:
            view.path.write_text(view.text, encoding="utf-8")
            written.append(view.path)
    db.close()
    return written


# ---- parsing ----------------------------------------------------------------

def parse_record(text: str):
    """Extract (kind, uuid) marker, title and {section_lower: raw} from an md.

    Managed header lines (Status:/fields/derived/changelog table) are ignored.
    """
    m = MARK_RE.search(text)
    marker = (m.group(1), m.group(2)) if m else (None, None)
    body = text[m.end():] if m else text
    tm = TITLE_RE.search(body)
    title = re.sub(r"^[\w-]+\.\s+", "", tm.group(1).strip()) if tm else ""
    matches = list(SECTION_RE.finditer(body))
    sections: dict[str, str] = {}
    for i, sm in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[sm.group(1).strip().lower()] = body[sm.end():end]
    return marker, title, sections


def normalize(text: str) -> str:
    """Format-only cleanup before storing: drop leading/trailing blank lines,
    compress runs of 2+ blank lines to 1 (outside code fences). Content as-is."""
    out: list[str] = []
    fence, blank = False, 0
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def md_status(text: str) -> str:
    m = STATUS_LINE_RE.search(text)
    return m.group(1).strip() if m else ""


# ---- mutations ---------------------------------------------------------------

def new_record(cfg: ProjConfig, kind: str, title: str,
               fields: dict[str, str] | None = None, spec: KindSpec | None = None):
    reg = load_registry(cfg.root)
    spec = spec or reg[kind]
    db = open_db(cfg)
    id_ = dbmod.next_id(spec, db)
    uuid = str(_uuid.uuid4())
    status = spec.status_allowed[0] if spec.status_allowed else ""
    created = id_ if spec.numbered == "date" else date.today().isoformat()
    db.insert(spec, uuid, id_, title, status, created, fields)
    db.add_log(spec.kind, uuid, "created", category="Added", when=created)
    row = db.get(spec, uuid, by="uuid")
    p = file_path(cfg, spec, row)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_record(spec, row, db), encoding="utf-8")
    db.close()
    render_all(cfg)  # keep views (TASKLIST etc.) in sync with the new record
    return row, p


def status_set(cfg: ProjConfig, kind: str, id_: str, status: str,
               when: str | None = None) -> dict:
    spec, db = spec_and_db(cfg, kind)
    row = by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        raise ValueError(f"{kind} {id_} not found")
    if not any(re.fullmatch(a.format(id=row["id"]), status) for a in spec.status_allowed):
        db.close()
        raise ValueError(f"status {status!r} not allowed for {kind} "
                         f"(allowed: {', '.join(spec.status_allowed)})")
    old = row["status"]
    db.set(spec, row["uuid"], status=status)
    if old != status:
        db.add_log(kind, row["uuid"], f"status {old} → {status}", "Changed", when)
    fresh = db.get(spec, row["uuid"], by="uuid")
    p = file_path(cfg, spec, fresh)
    p.write_text(render_record(spec, fresh, db), encoding="utf-8")
    db.close()
    render_all(cfg)  # status changes affect rendered views
    return fresh


def add_log(cfg: ProjConfig, kind: str, id_: str, summary: str,
            category: str | None = None) -> None:
    spec, db = spec_and_db(cfg, kind)
    row = by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        raise ValueError(f"{kind} {id_} not found")
    cat, text = dbmod.parse_summary(summary)
    db.add_log(kind, row["uuid"], text, category or cat)
    fresh = db.get(spec, row["uuid"], by="uuid")
    p = file_path(cfg, spec, fresh)
    p.write_text(render_record(spec, fresh, db), encoding="utf-8")
    db.close()


# ---- task checkboxes ----------------------------------------------------------

def checkboxes(section_text: str) -> list[tuple[int, bool, str]]:
    """[(indent_level, checked, text)] for every checkbox line (any level)."""
    out = []
    for line in section_text.splitlines():
        m = CHECKBOX_RE.match(line)
        if m:
            indent = len(line) - len(line.lstrip())
            out.append ((indent // 2, m.group(1).lower() == "x", line.strip()))
    return out


def has_checked(section_text: str) -> bool:
    return any(c for _, c, _ in checkboxes(section_text))


def all_checked(section_text: str) -> bool:
    return all(c for _, c, _ in checkboxes(section_text))


def _task_sections(row: dict) -> dict:
    return json.loads(row["sections"] or "{}")


# ---- sync / adopt --------------------------------------------------------------

def _ingest_md(cfg: ProjConfig, spec: KindSpec, db: dbmod.Db, p: Path,
               strict: bool) -> tuple[list[str], list[str]]:
    """Read one md into the DB. strict=True (sync): uuid marker required.
    Returns (problems, notes)."""
    text = p.read_text(encoding="utf-8")
    (mk, u), title, raw_sections = parse_record(text)
    if strict:
        if mk != spec.kind or not u:
            return [f"{p.name}: missing/foreign uuid marker — run `proj adopt`"], []
        row = db.get(spec, u, by="uuid")
        if row is None:
            return [f"{p.name}: uuid {u} not in DB — run `proj adopt`"], []
        if not p.name.startswith(row["id"] + "-"):
            return [f"{p.name}: id {row['id']!r} does not match filename"], []
        notes = []
        if title and title != row["title"]:
            notes.append(f"{p.name}: title drift in md (DB wins: {row['title']!r})")
        if md_status(text) not in ("", row["status"]):
            notes.append(f"{p.name}: status drift in md (DB wins: {row['status']!r})")
        sections = {s.name.lower(): normalize(raw_sections.get(s.name.lower(), ""))
                    for s in spec.sections if not s.view}
        db.set(spec, u, sections=json.dumps(sections, ensure_ascii=False))
        # task: first checkbox tick auto-promotes Not started → In progress
        if spec.kind == "task" and row["status"] == "Not started" \
                and has_checked(sections.get("steps", "")):
            db.set(spec, u, status="In progress")
            db.add_log(spec.kind, u, "status Not started → In progress", "Changed")
            notes.append(f"{p.name}: auto status Not started → In progress")
        return [], notes
    # adopt mode
    if mk == spec.kind and u and db.get(spec, u, by="uuid"):
        return [], []  # already in DB
    m = _id_prefix_re(spec).match(p.name)
    if not m:
        return [f"{p.name}: filename does not start with a valid id "
                f"for kind {spec.kind!r}"], []
    id_ = m.group(1)
    if db.get(spec, id_, by="id"):
        return [f"{p.name}: duplicate id {id_!r} in {spec.kind} — resolve manually"], []
    if not title:
        return [f"{p.name}: no '# Title' heading — cannot adopt"], []
    # headings often repeat the id prefix ("# 0004 Some Title") — strip it
    title = re.sub(rf"^{re.escape(id_)}[.\s]\s*", "", title)
    uuid = u or str(_uuid.uuid4())
    status = spec.status_allowed[0] if spec.status_allowed else ""
    created = id_ if spec.numbered == "date" else date.today().isoformat()
    db.insert(spec, uuid, id_, title, status, created, {})
    db.add_log(spec.kind, uuid, "adopted into proj", category="Added", when=created)
    sections = {s.name.lower(): normalize(raw_sections.get(s.name.lower(), ""))
                for s in spec.sections if not s.view}
    db.set(spec, uuid, sections=json.dumps(sections, ensure_ascii=False))
    if spec.kind == "task" and has_checked(sections.get("steps", "")):
        db.set(spec, uuid, status="In progress")
        db.add_log(spec.kind, uuid, "status Not started → In progress", "Changed")
    # canonical filename is id + slugify(title); if the user's file used a
    # different slug, move it so only the rendered name remains
    canonical = cfg.root / spec.dir / (id_ + "-" + slugify(title) + ".md")
    if p != canonical and not canonical.exists():
        canonical.parent.mkdir(parents=True, exist_ok=True)
        p.rename(canonical)
    return [], [f"{p.name}: adopted as {spec.kind} {id_} (uuid {uuid[:8]}…)"
                + ("" if u else ", uuid generated")]


def _scan_files(cfg: ProjConfig, spec: KindSpec, files: list[Path] | None):
    if files:
        return [f for f in files if f.parent == cfg.root / spec.dir]
    d = cfg.root / spec.dir
    return sorted(d.glob("*.md")) if d.is_dir() else []


def sync(cfg: ProjConfig, files: list[Path] | None = None):
    """md → DB (validated, normalized) → render all md. Returns (problems, notes)."""
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    problems, notes = [], []
    for spec in reg.values():
        for p in _scan_files(cfg, spec, files):
            pr, nt = _ingest_md(cfg, spec, db, p, strict=True)
            problems += pr
            notes += nt
    if not problems:
        render_all(cfg)
    db.close()
    return problems, notes


def adopt(cfg: ProjConfig, files: list[Path] | None = None):
    """Absorb existing md files (uuid preferred; missing uuid → id check +
    generate). Then render everything. Returns (problems, notes)."""
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    problems, notes = [], []
    for spec in reg.values():
        for p in _scan_files(cfg, spec, files):
            pr, nt = _ingest_md(cfg, spec, db, p, strict=False)
            problems += pr
            notes += nt
    if not problems:
        render_all(cfg)
    db.close()
    return problems, notes


# ---- info ----------------------------------------------------------------------

CHEATSHEET = """\
Ops cheatsheet:
  new:        proj new <kind> TITLE [--field name=value]
  change:     proj status <kind> ID STATUS | proj log <kind> ID "Category: …"
  md edited:  proj sync | proj adopt (first time, unmanaged files)
  query:      proj list <kind> | proj show <kind> ID [--history]
  overview:   proj info [--json]"""


def info(cfg: ProjConfig) -> dict:
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    kinds = {}
    for kind, spec in reg.items():
        if not db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (kind,)).fetchone():
            continue
        rows = db.all(spec)
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        num = ("letter" if spec.numbered == "letter"
               else "date" if spec.numbered == "date"
               else "NNNN" if isinstance(spec.numbered, int) else "none")
        kinds[kind] = {
            "count": len(rows), "dir": spec.dir, "numbered": num,
            "statuses": by_status,
        }
    cl = db.cl_entries()
    versions = sorted({e["version"] for e in cl})
    views = ["TASKLIST.md", "CHANGELOG.md"]
    if "plan" in kinds:
        views.append("plan 'Task List' sections")
    out = {
        "root": str(cfg.root),
        "kinds": kinds,
        "changelog": {"entries": len(cl), "versions": versions,
                      "latest": versions[-1] if versions else None},
        "views": views,
        "cheatsheet": CHEATSHEET.splitlines(),
    }
    db.close()
    return out


def info_text(d: dict) -> str:
    lines = [f"Project: {d['root']} — managed by proj (.proj.sqlite)", ""]
    for kind, k in d["kinds"].items():
        lines.append(f"  {kind:<11} {k['count']:<3} record(s)  @ {k['dir']}/  [{k['numbered']}]")
        if k["statuses"]:
            lines.append("              " + ", ".join(
                f"{s} {n}" for s, n in sorted(k["statuses"].items())))
    cl = d["changelog"]
    if cl["entries"]:
        lines.append(f"Changelog: {cl['entries']} entries, latest {cl['latest']}")
    lines.append("Views (rendered, never hand-edit): " + ", ".join(d["views"]))
    lines.append("")
    lines.append(CHEATSHEET)
    return "\n".join(lines) + "\n"
