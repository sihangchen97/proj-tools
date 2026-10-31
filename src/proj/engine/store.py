"""Record storage engine (module-level functions only): YAML template (schema) +
SQLite (storage) + md (input channel). The *behavior* layer (Record class
hierarchy, record_for) lives in record.py — this module calls back lazily.

md file shape (rendered by the engine):

    <!-- proj:decision 3f2a1b4c-... -->    <- line 1: tool-owned uuid marker (a comment,
                                             <-   invisible in preview)
    # 0001. Title                          <- tool-owned heading

    > **status:** 📛 Deprecated             <- tool-owned metadata block (a blockquote,
    > **supersedes:** [0001](0001-x.md)    <-   NOT front matter — no md frontmatter
    > **subtask:** B                        <-   validation; renders as a gray sidebar,
    > **started:** —                        <-   each line hard-wrapped; same-type id
    > **done:** 2026-10-29                 <-   refs (supersedes/superseded_by) link

    Summary: one sentence                  <- below the box; md wins on sync

    | Date | Category | Change |          <- latest 5 changelog entries (rendered)
    |---|---|---|
    | 2026-10-29 | Changed | status Proposed → Accepted |

    ## Context                             <- user-owned content (the only
    ...                                    <- part humans/LLMs edit)

The metadata block is a blockquote of `> **key:** value` lines — NOT a front
matter, so md frontmatter validators leave it alone. Each line ends with two
spaces (markdown hard wrap). Same-type record id refs (supersedes /
superseded_by, a task's parent / subtask) render as `[id](path)` links to the
target's md; the uuid marker comment sits on line 1, above the block.

sync: md → DB (sections win) → render all md (tool-owned parts win).
"""
from __future__ import annotations

import json
import re
import uuid as _uuid
from datetime import date
from pathlib import Path

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.core.templates import RecordTypeSpec, load_registry, slugify

# tool-owned uuid marker (an html-comment line, on line 1 above the blockquote head)
MARK_RE = re.compile(
    r"^<!-- proj:([\w-]+) "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) -->[ \t]*$",
    re.MULTILINE)
TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
CHECKBOX_RE = re.compile(r"^\s*[-*] \[([ xX])\]")
# template fields whose values are record-id refs (rendered as links in the head)
RELATION_FIELDS = {"supersedes", "superseded_by", "parent", "related"}
# one head line: `> **key:** value` (+ trailing two spaces = markdown hard wrap)
HEAD_LINE_RE = re.compile(r"^> \*\*(\w+):\*\*\s*(.*)$")


def open_db(cfg: ProjConfig) -> dbmod.Db:
    return dbmod.Db(cfg.db_path)


def spec_and_db(cfg: ProjConfig, record_type: str):
    reg = load_registry(cfg.root)
    if record_type not in reg:
        raise ValueError(f"unknown record_type {record_type!r} "
                         f"(registered: {', '.join(sorted(reg))})")
    return reg[record_type], open_db(cfg)


def by_id(cfg: ProjConfig, spec: RecordTypeSpec, db: dbmod.Db, id_: str) -> dict | None:
    row = db.get(spec, id_, by="id")
    if row is None and id_.isdigit() and isinstance(spec.numbered, int):
        row = db.get(spec, f"{int(id_):0{spec.numbered}d}", by="id")
    return row


def _display_id(spec: RecordTypeSpec, id_: str) -> str:
    return str(int(id_)) if id_.isdigit() else id_


def _id_prefix_re(spec: RecordTypeSpec) -> re.Pattern:
    if spec.numbered == "letter":
        return re.compile(r"^([A-Z]+)-")
    if isinstance(spec.numbered, int):
        return re.compile(rf"^(\d{{{spec.numbered}}})-")
    return re.compile(r"^(\S+)-")


def file_path(cfg: ProjConfig, spec: RecordTypeSpec, row: dict) -> Path:
    return cfg.root / spec.dir / (row["id"] + "-" + slugify(row["title"]) + ".md")


# ---- rendering -------------------------------------------------------------

def _fm_value(v: str) -> str:
    """Quote a front-matter value only when needed (colons, quotes, leading
    dashes) — plain otherwise, so the gray box stays human-readable."""
    if re.match(r'^[^\s"\'#][^:\'\']*$', v) and v.strip() == v:
        return v
    return json.dumps(v, ensure_ascii=False)


def _link_value(value: str, db: dbmod.Db, registry: dict) -> str:
    """Render comma/whitespace-separated record ids as `[id](relpath)` markdown
    links to the target's md (dangling ids fall back to plain text). Resolves
    across any record_type so a task's parent→plan link works too. The link path
    is relative to the project root (db.path.parent)."""
    root = Path(db.path).parent
    ids = [x for x in value.replace(",", " ").split() if x]
    out = []
    for i in ids:
        target = None
        for spec in registry.values():
            r = by_id(ProjConfig(root), spec, db, i)
            if r is not None:
                target = (spec, r)
                break
        if target is None:
            out.append(i)
            continue
        spec, r = target
        p = file_path(ProjConfig(root), spec, r).relative_to(root)
        out.append(f"[{i}]({p.as_posix()})")
    return ", ".join(out) if out else value


def _fm_block(spec: RecordTypeSpec, row: dict, fm: dict) -> str:
    """Tool-owned metadata block: a blockquote of `> **key:** value` lines (a
    gray sidebar, NOT a front matter, so md frontmatter validators leave it
    alone), rendered below the record title. Each line ends with two spaces
    (markdown hard wrap). Same-type id refs are pre-linked."""
    return "\n".join(f"> **{k}:** {_fm_value(v)}  " for k, v in fm.items())


def render_record(spec: RecordTypeSpec, row: dict, db: dbmod.Db,
                  registry: dict[str, RecordTypeSpec] | None = None) -> str:
    # tool-owned head: uuid marker comment (line 1) + blockquote metadata
    # (status/fields/subtask/derived, id refs as links); the one-sentence
    # summary lives BELOW the box (between the heading and the changelog table).
    reg = registry if registry is not None else load_registry(Path(db.path).parent)
    fm = {
        "status": spec.status_display(row["status"]),
    }
    for f in spec.fields:
        v = row.get(f.name) or ""
        if v:
            fm[f.name] = _link_value(v, db, reg) if f.name in RELATION_FIELDS else v
    if spec.record_type == "task" and "parent" in row:
        from proj.engine.views import _norm_id  # lazy: views↔records
        kids = [r for r in db.all(spec)
                if _norm_id((r.get("parent") or "").strip()) == _norm_id(row["id"])
                and r["id"] != row["id"]]
        if kids:
            fm["subtask"] = _link_value(", ".join(k["id"] for k in kids), db, reg)
    for d in spec.derived:
        # derived rows always show (placeholder '—' until the event happened)
        v = db.first_date_to(spec.record_type, row["uuid"], d.to_status)
        fm[d.name.lower()] = v or "—"
    # marker comment (line 1, invisible) → title → metadata block → summary
    lines = [f"<!-- proj:{spec.record_type} {row['uuid']} -->",
             f"# {_display_id(spec, row['id'])}. {row['title']}", "",
             _fm_block(spec, row, fm).rstrip("\n"),
             f"Summary: {row.get('summary') or '—'}", ""]
    logs = db.logs(spec.record_type, row["uuid"], limit=5)
    if logs:
        lines += ["| Date | Category | Change |", "|---|---|---|"]
        lines += [f"| {e['date']} | {e['category']} | {e['summary']} |" for e in logs]
        lines.append("")
    sections = json.loads(row["sections"] or "{}")
    for sec in spec.sections:
        lines += [f"## {sec.name}", ""]
        if sec.kind == "task":
            # rendered task subtree: tasks whose parent is this record (DB link),
            # recursively — template data drives the rendering, no class needed
            from proj.engine.views import _norm_id, task_tree_lines  # lazy: views↔store cycle
            task_spec = (registry or load_registry(Path(db.path).parent.parent)).get("task")
            lines += (task_tree_lines(
                db.all(task_spec) if task_spec else [], None, _norm_id(row["id"]))
                or ["(no tasks attached)"])
        else:
            lines.append(sections.get(sec.name.lower(), "").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def md_drift(cfg: ProjConfig) -> list[str]:
    """md files whose section content differs from the DB (unsynced edits).
    Used to warn before DB-only renders silently overwrite them."""
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    out: list[str] = []
    for spec in reg.values():
        d = cfg.root / spec.dir
        if not d.is_dir():
            continue
        for p in sorted(x for x in d.glob("*.md") if not x.name.startswith("_")):
            text = p.read_text(encoding="utf-8")
            (mk, u), _t, raw_sections = parse_record(text)
            if mk != spec.record_type or not u:
                continue
            row = db.get(spec, u, by="uuid")
            if row is None:
                continue
            stored = json.loads(row["sections"] or "{}")
            for s in spec.sections:
                if s.kind != "text":
                    continue
                if normalize(raw_sections.get(s.name.lower(), "")) != \
                        stored.get(s.name.lower(), ""):
                    out.append(p.name)
                    break
    db.close()
    return out


def render_all(cfg: ProjConfig) -> list[Path]:
    """Phase 2: DB → every record file + global views. Returns written paths."""
    from proj.engine.views import render_changelog_md, render_tasklist_md
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
    """Extract (record_type, uuid) marker, title and {section_lower: raw} from an md.

    The tool-owned head (uuid marker comment + a blockquote of `> **key:** value`
    lines) sits ABOVE the title and never matches the title/section patterns, so
    the whole text is scanned directly.
    """
    m = MARK_RE.search(text)
    marker = (m.group(1), m.group(2)) if m else (None, None)
    tm = TITLE_RE.search(text)
    title = re.sub(r"^[\w-]+\.\s+", "", tm.group(1).strip()) if tm else ""
    matches = list(SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, sm in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[sm.group(1).strip().lower()] = text[sm.end():end]
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


def _strip_links(v: str) -> str:
    """`[id](path)` → `id` (markdown links back to plain record ids)."""
    return re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", v).strip()


def fm_fields(text: str) -> dict:
    """Tool-owned head fields of a record md ({} when absent).

    The head is a blockquote of `> **key:** value` lines — parsed line by line
    (not yaml.safe_load: PyYAML chokes on 4-byte emoji values). Markdown links
    in id-ref values are stripped back to plain ids.
    """
    out: dict = {}
    for raw in text.splitlines():
        m = HEAD_LINE_RE.match(raw.strip())
        if not m:
            continue
        out[m.group(1)] = _strip_links(m.group(2))
    return out


def md_status(text: str) -> str:
    """Status from the front matter (leading emoji stripped)."""
    s = str(fm_fields(text).get("status") or "")
    # drop a leading emoji: status_display renders "<emoji> <name>";
    # an emoji is always non-ASCII, a status name always ASCII
    parts = s.split(" ", 1)
    if len(parts) == 2 and not parts[0].isascii():
        return parts[1].strip()
    return s


def md_summary(text: str) -> str:
    fm = fm_fields(text)
    if "summary" in fm:
        return str(fm["summary"]).strip()
    m = re.search(r"^Summary:\s*(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


# ---- mutations ---------------------------------------------------------------

def new_record(cfg: ProjConfig, record_type: str | RecordTypeSpec, title: str,
               summary: str = "",
               fields: dict[str, str] | None = None, spec: RecordTypeSpec | None = None):
    if isinstance(record_type, RecordTypeSpec):
        spec = record_type
    else:
        reg = load_registry(cfg.root)
        spec = spec or reg[record_type]
    db = open_db(cfg)
    id_ = dbmod.next_id(spec, db)
    uuid = str(_uuid.uuid4())
    status = spec.status_allowed[0] if spec.status_allowed else ""
    created = date.today().isoformat()
    db.insert(spec, uuid, id_, title, status, created, fields, summary)
    db.add_log(spec.record_type, uuid, "created", category="Added", when=created)
    row = db.get(spec, uuid, by="uuid")
    p = file_path(cfg, spec, row)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_record(spec, row, db), encoding="utf-8")
    db.close()
    render_all(cfg)  # keep views (TASKLIST etc.) in sync with the new record
    return row, p


def status_set(cfg: ProjConfig, record_type: str, id_: str, status: str,
               when: str | None = None) -> dict:
    spec, db = spec_and_db(cfg, record_type)
    row = by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        raise ValueError(f"{record_type} {id_} not found")
    if not any(re.fullmatch(a.format(id=row["id"]), status) for a in spec.status_allowed):
        db.close()
        raise ValueError(f"status {status!r} not allowed for {record_type} "
                         f"(allowed: {', '.join(spec.status_allowed)})")
    old = row["status"]
    db.set(spec, row["uuid"], status=status)
    if old != status:
        db.add_log(record_type, row["uuid"], f"status {old} → {status}", "Changed", when)
    fresh = db.get(spec, row["uuid"], by="uuid")
    p = file_path(cfg, spec, fresh)
    p.write_text(render_record(spec, fresh, db), encoding="utf-8")
    db.close()
    render_all(cfg)  # status changes affect rendered views
    return fresh


def add_log(cfg: ProjConfig, record_type: str, id_: str, summary: str,
            category: str | None = None) -> None:
    spec, db = spec_and_db(cfg, record_type)
    row = by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        raise ValueError(f"{record_type} {id_} not found")
    from proj.core.templates import load_changelog_spec  # lazy: avoid cycle
    cat, text = dbmod.parse_summary(summary, load_changelog_spec(cfg.root))
    db.add_log(record_type, row["uuid"], text, category or cat)
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




# ---- sync / adopt --------------------------------------------------------------

def _ingest_md(cfg: ProjConfig, spec: RecordTypeSpec, db: dbmod.Db, p: Path,
               strict: bool) -> tuple[list[str], list[str]]:
    """Read one md into the DB. strict=True (sync): uuid marker required.
    Returns (problems, notes)."""
    text = p.read_text(encoding="utf-8")
    (mk, u), title, raw_sections = parse_record(text)
    if strict:
        if mk != spec.record_type or not u:
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
                    for s in spec.sections if s.kind == "text"}
        db.set(spec, u, sections=json.dumps(sections, ensure_ascii=False))
        # Summary: md wins (hand-written result/progress), DB otherwise
        db.set(spec, u, summary=md_summary(text) or (row.get("summary") or ""))
        # record_type-specific ingest side effects (task: checkbox auto-promotion)
        from proj.engine.record import record_for  # lazy: record↔store cycle
        record_for(spec).on_ingest(db, row, sections, notes, p.name)
        return [], notes
    # adopt mode
    if mk == spec.record_type and u and db.get(spec, u, by="uuid"):
        return [], []  # already in DB
    m = _id_prefix_re(spec).match(p.name)
    if not m:
        return [f"{p.name}: filename does not start with a valid id "
                f"for record_type {spec.record_type!r}"], []
    id_ = m.group(1)
    if db.get(spec, id_, by="id"):
        return [f"{p.name}: duplicate id {id_!r} in {spec.record_type} — resolve manually"], []
    if not title:
        return [f"{p.name}: no '# Title' heading — cannot adopt"], []
    # headings often repeat the id prefix ("# 0004 Some Title") — strip it
    title = re.sub(rf"^{re.escape(id_)}[.\s]\s*", "", title)
    uuid = u or str(_uuid.uuid4())
    status = spec.status_allowed[0] if spec.status_allowed else ""
    created = date.today().isoformat()
    db.insert(spec, uuid, id_, title, status, created, {}, md_summary(text))
    db.add_log(spec.record_type, uuid, "adopted into proj", category="Added", when=created)
    sections = {s.name.lower(): normalize(raw_sections.get(s.name.lower(), ""))
                for s in spec.sections if s.kind == "text"}
    db.set(spec, uuid, sections=json.dumps(sections, ensure_ascii=False))
    # adopt: same record_type hook, fresh row; promotion notes are dropped
    from proj.engine.record import record_for  # lazy: record↔store cycle
    record_for(spec).on_ingest(db, db.get(spec, uuid, by="uuid"),
                               sections, [], p.name)
    # canonical filename is id + slugify(title); if the user's file used a
    # different slug, move it so only the rendered name remains
    canonical = cfg.root / spec.dir / (id_ + "-" + slugify(title) + ".md")
    if p != canonical and not canonical.exists():
        canonical.parent.mkdir(parents=True, exist_ok=True)
        p.rename(canonical)
    return [], [f"{p.name}: adopted as {spec.record_type} {id_} (uuid {uuid[:8]}…)"
                + ("" if u else ", uuid generated")]


def _scan_files(cfg: ProjConfig, spec: RecordTypeSpec, files: list[Path] | None):
    """Record files in a record_type dir. `_`-prefixed files are rendered views
    (e.g. docs/decisions/_TOC.md) — never records, never hand-edited."""
    if files:
        return [f for f in files if f.parent == cfg.root / spec.dir
                and not f.name.startswith("_")]
    d = cfg.root / spec.dir
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if not p.name.startswith("_"))


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
  new:        proj new <record_type> TITLE [--field name=value]
  change:     proj status <record_type> ID STATUS | proj log <record_type> ID "Category: …"
  md edited:  proj sync | proj adopt (first time, unmanaged files)
  query:      proj list <record_type> | proj show <record_type> ID [--history]
  overview:   proj info [--json]"""


def info(cfg: ProjConfig) -> dict:
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    record_types = {}
    for record_type, spec in reg.items():
        if not db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (record_type,)).fetchone():
            continue
        rows = db.all(spec)
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        num = ("letter" if spec.numbered == "letter"
               else "NNNN" if isinstance(spec.numbered, int) else "none")
        record_types[record_type] = {
            "count": len(rows), "dir": spec.dir, "numbered": num,
            "statuses": by_status,
        }
    cl = db.cl_entries()
    versions = sorted({e["version"] for e in cl})
    views = ["TASKLIST.md", "CHANGELOG.md"]
    if "plan" in record_types:
        views.append("plan 'Task List' sections")
    out = {
        "root": str(cfg.root),
        "record_types": record_types,
        "changelog": {"entries": len(cl), "versions": versions,
                      "latest": versions[-1] if versions else None},
        "views": views,
        "cheatsheet": CHEATSHEET.splitlines(),
    }
    db.close()
    return out


def info_text(d: dict) -> str:
    lines = [f"Project: {d['root']} — managed by proj (.proj.sqlite)", ""]
    for rt, k in d["record_types"].items():
        lines.append(f"  {rt:<11} {k['count']:<3} record(s)  @ {k['dir']}/  [{k['numbered']}]")
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
