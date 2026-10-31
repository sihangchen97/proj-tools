"""Record storage engine (module-level functions only): YAML template (schema) +
SQLite (storage) + md (input channel). The *behavior* layer (Record class
hierarchy, record_for) lives in record.py — this module calls back lazily.

md file shape (rendered by the engine):

    <!-- proj:decision DEC-0001 -->        <- line 1: tool-owned id marker (a comment,
                                             <-   invisible in preview)
    <!-- proj:head:start … TOOL-MANAGED … -->  <- head fence (HEAD_START): everything
    # 0001. Title                          <-   between start and end is maintained by
                                             <-   `proj` (title + metadata block + summary);
                                             <-   the start line says: never hand-edit
    > **status:** 📛 Deprecated             <- tool-owned metadata block (a blockquote,
    > **supersedes:** [0001](0001-x.md)    <-   not front matter, so md validators
    > **subtask:** B                        <-   leave it alone; renders as a gray
    > **created:** 2026-10-29              <-   sidebar, every line but the last
    > **done:** 2026-10-29                 <-   hard-wrapped; same-type id refs
                                             <-   link
    _one sentence summary_                 <- optional italic line below the block
                                             <-   (shown only when set; md wins)
    <!-- proj:head:end -->                 <- head fence end (HEAD_END): region owned

    | Date | Category | Change |          <- changelog entries (latest PROJ_LOGS
    |---|---|---|                         <-   rows; 0=none, -1=all, unset=5),
    | 2026-10-29 | Changed | status Proposed → Accepted |  <- status changes from columns
    * emoji legend ...                     <- status legend line below the table

    ## Context                             <- user-owned content (the only
    ...                                    <- part humans/LLMs edit)

The metadata block is a blockquote of `> **key:** value` lines — NOT a front
matter, so md frontmatter validators leave it alone. Every line but the last
ends with two spaces (markdown hard wrap — a hard wrap on the last line would
pull the following content into the blockquote). Record id refs (supersedes /
superseded_by / related / a task's parent / subtask) render as `[filename](path)`
links: the text is the target's full filename, the path relative to this
record's own file (same dir → bare filename, across dirs → ../dir/filename);
one ref = one line, several = a bare key line + `> - item` list. The
`kind: task` section renders the editable task tree (children via the
kind=parent relation), outside the head fence. The id marker comment sits on
line 1, above the block.

sync: md → DB (sections win) → render all md (tool-owned parts win).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.core.templates import RecordTypeSpec, load_registry, slugify

# tool-owned id marker (an html-comment line, on line 1 above the blockquote head)
# id shape: 1-3 letters + '-' + digits, letters+digits == 7 (8 chars total),
# e.g. DEC-0001, T-000001. Case-insensitive, not glued to other alnums,
# uppercased on use. MARK_RE wraps this shape in the line-1 head comment.
_ID_SHAPE = (
    r"(?<![0-9A-Za-z])([A-Za-z]-[0-9]{6}|[A-Za-z]{2}-[0-9]{5}"
    r"|[A-Za-z]{3}-[0-9]{4})(?![0-9A-Za-z])"
)
MARK_RE = re.compile(rf"^<!-- proj:([\w-]+) {_ID_SHAPE} -->[ \t]*$", re.MULTILINE)
TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
CHECKBOX_RE = re.compile(r"^\s*[-*] \[([ xX])\]")
# one head line: `> **key:** value` (+ trailing two spaces = markdown hard wrap)
HEAD_LINE_RE = re.compile(r"^> \*\*(\w+):\*\*\s*(.*)$")
# head fence: an invisible comment pair marks the tool-owned head region
# (id marker above, title + metadata block + optional summary inside). The
# start line carries a human/agent-readable instruction that the region is
# maintained by `proj` and must not be hand-edited (edit via `proj` instead).
HEAD_START = "<!-- proj:head:start — TOOL-MANAGED: title + metadata + summary are " \
             "maintained by `proj`; never hand-edit this region (use `proj`) -->"
HEAD_END = "<!-- proj:head:end -->"


def open_db(cfg: ProjConfig) -> dbmod.Db:
    return dbmod.Db(cfg.db_path)


def spec_and_db(cfg: ProjConfig, record_type: str):
    reg = load_registry(cfg.root)
    if record_type not in reg:
        raise ValueError(f"unknown record_type {record_type!r} "
                         f"(registered: {', '.join(sorted(reg))})")
    return reg[record_type], open_db(cfg)


def by_id(cfg: ProjConfig, spec: RecordTypeSpec, db: dbmod.Db, id_: str) -> dict | None:
    row = db.get(spec, id_)
    if row is not None:
        return row
    # tolerant: a bare number ("1"/"0001") resolves to the canonical {code}-{n:0w}
    if isinstance(spec.numbered, int) and re.fullmatch(r"0*\d+", id_ or ""):
        return db.get(spec, f"{spec.code}-{int(id_):0{spec.numbered}d}")
    return None


def _display_id(spec: RecordTypeSpec, id_: str) -> str:
    return id_  # stored canonical ({code}{n:0w}); shown as-is


def canonical_id(cfg: ProjConfig, ref: str) -> str:
    """Resolve a cross-type ref (task parent) to a canonical id (any record_type).

    Exact canonical ids pass through. A bare number ("1") is widened to each
    record_type's id form; a unique match wins, an ambiguous one is rejected
    with a hint to pass the canonical id (PLN-0001)."""
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    matches: list[str] = []
    try:
        for spec in reg.values():
            if db.get(spec, ref) is not None:
                return ref
        if re.fullmatch(r"0*\d+", ref):
            for spec in reg.values():
                if isinstance(spec.numbered, int):
                    cand = f"{spec.code}-{int(ref):0{spec.numbered}d}"
                    if db.get(spec, cand) is not None:
                        matches.append(cand)
        if len(matches) == 1:
            return matches[0]
    finally:
        db.close()
    if len(matches) > 1:
        raise ValueError(f"{ref!r} matches several records ({', '.join(matches)}) "
                         f"— use the canonical id (e.g. {matches[0]})")
    raise ValueError(f"{ref!r} does not match any record "
                     f"— use the canonical id (e.g. PLN-0001)")


def ref_record(cfg: ProjConfig, db: dbmod.Db, ref: str):
    """(spec, row) for a ref across every record_type (None, None when no match).
    An exact canonical id wins; a bare number ("1") is widened to each type's
    id form (a task's parent may point at a plan) — an ambiguous bare number
    raises: pass the canonical id (e.g. DEC-0001)."""
    reg = load_registry(cfg.root)
    for spec in reg.values():
        r = db.get(spec, ref)
        if r is not None:
            return spec, r
    if re.fullmatch(r"0*\d+", ref):
        matches: list[tuple] = []
        for spec in reg.values():
            if isinstance(spec.numbered, int):
                r = db.get(spec, f"{spec.code}-{int(ref):0{spec.numbered}d}")
                if r is not None:
                    matches.append((spec, r))
        if len(matches) > 1:
            raise ValueError(f"{ref!r} matches several records "
                             f"({', '.join(s.record_type + ' ' + r['id'] for s, r in matches)}) "
                             "\u2014 use the canonical id (e.g. DEC-0001)")
        if matches:
            return matches[0]
    return None, None


def _id_prefix_re(spec: RecordTypeSpec) -> re.Pattern:
    """Filename prefix: the spec's own id (code + '-' + N digits, case-insensitive,
    not glued to alnums) followed by the '-' of the canonical name."""
    if isinstance(spec.numbered, int) and spec.code:
        pre, n = re.escape(spec.code) + "-", f"[0-9]{{{spec.numbered}}}"
    else:
        pre, n = r"[A-Za-z]{1,3}-", r"[0-9]{4,6}"
    return re.compile(
        rf"^((?<![0-9A-Za-z]){pre}{n}(?![0-9A-Za-z]))-", re.IGNORECASE)


def file_path(cfg: ProjConfig, spec: RecordTypeSpec, row: dict) -> Path:
    return cfg.root / spec.dir / (row["id"] + "-" + slugify(row["title"]) + ".md")


# ---- rendering -------------------------------------------------------------

def _head_value(v: str) -> str:
    """Quote a head value only when needed (colons, quotes, leading dashes) —
    plain otherwise, so the gray sidebar stays human-readable."""
    if re.match(r'^[^\s"\'#][^:\'\']*$', v) and v.strip() == v:
        return v
    return json.dumps(v, ensure_ascii=False)


def _link_value(value: str, db: dbmod.Db, registry: dict,
                spec: RecordTypeSpec, row: dict) -> str | list[str]:
    """Render comma/whitespace-separated record ids as markdown links: the
    link TEXT is the target's filename without the .md extension
    (id-slug) and the path is relative to this record's own file (same dir
    → bare filename, across dirs → ../target-dir/filename). Dangling ids
    fall back to plain text.
    One id → a single string; several → a list (the head renders it as a
    blockquote item list). Resolves across any record_type (parent→plan)."""
    root = Path(db.path).parent
    src_file = root / spec.dir / (row["id"] + "-" + slugify(row["title"]) + ".md")
    ids = [x for x in value.replace(",", " ").split() if x]
    out = []
    for i in ids:
        target = None
        for tspec in registry.values():
            r = by_id(ProjConfig(root), tspec, db, i)
            if r is not None:
                target = (tspec, r)
                break
        if target is None:
            out.append(i)
            continue
        tspec, r = target
        tgt = file_path(ProjConfig(root), tspec, r)
        rel = os.path.relpath(tgt, src_file.parent).replace(os.sep, "/")
        out.append(f"[{tgt.stem}]({rel})")
    return out[0] if len(out) == 1 else out


def _done_date(spec: RecordTypeSpec, row: dict, db: dbmod.Db) -> str:
    """The earliest date this record first entered ANY status with `done: true`
    ('' until then). Entering one done status counts even if it later left it."""
    dates = [db.first_date_to(row["id"], s) for s in spec.status_done]
    dates = [d for d in dates if d]
    return min(dates) if dates else ""


def _logs_limit() -> int | None:
    """PROJ_LOGS: how many changelog rows to render per record md.
    0 = none, -1 = all, N = latest N; unset/empty = 5 (the old default)."""
    raw = (os.environ.get("PROJ_LOGS") or "").strip()
    if not raw:
        return 5
    n = int(raw)
    return None if n < 0 else n


def _log_change(e: dict, spec: RecordTypeSpec) -> str:
    """The Change column (D5): a status change renders as an emoji-prefixed
    '<from> → <to>' leading the optional comment; any other event shows its
    comment alone (both status columns NULL)."""
    if e["from_status"] and e["to_status"]:
        change = (f"{spec.status_display(e['from_status'])} → "
                  f"{spec.status_display(e['to_status'])}")
        if e["comment"]:
            change += f" {e['comment']}"
        return change
    return e["comment"] or "—"


def _head_lines(fm: dict) -> list[str]:
    """Tool-owned head lines: a blockquote of `> **key:** value` (a gray
    sidebar). A value is either a plain string (one line) or a list of
    strings (a bare key line + `> - item` lines, one per value)."""
    lines: list[str] = []
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"> **{k}:**")
            lines += [f"> - {_head_value(x)}" for x in v]
        else:
            lines.append(f"> **{k}:** {_head_value(v)}")
    return lines


def render_record(spec: RecordTypeSpec, row: dict, db: dbmod.Db,
                  registry: dict[str, RecordTypeSpec] | None = None) -> str:
    # tool-owned head: id marker comment (line 1) + blockquote metadata
    # (status/created/done rows + relation rows, id refs as links to the
    # target's full filename); the one-sentence summary is an italic line
    # below the block (shown only when set — never a "Summary:" label).
    # contain_task records get a subtask row (direct children via relations);
    # the task TREE itself stays in their kind: task section (editable block).
    reg = registry if registry is not None else load_registry(Path(db.path).parent)
    fm = {
        "status": spec.status_display(row["status"]),
        "created": row.get("created") or "—",
    }
    done = _done_date(spec, row, db)
    if done:
        fm["done"] = done
    if row.get("commit"):
        fm["commit"] = row["commit"]
    sup = [e["target"] for e in db.supersede_of(row["id"])]
    if sup:
        fm["supersedes"] = _link_value(", ".join(sup), db, reg, spec, row)
    sup_by = [e["source"] for e in db.superseded_by_of(row["id"])]
    if sup_by:
        fm["superseded_by"] = _link_value(", ".join(sup_by), db, reg, spec, row)
    rel = db.related_of(row["id"])
    if rel:
        fm["related"] = _link_value(", ".join(rel), db, reg, spec, row)
    if spec.record_type == "task":
        par = db.parent_of(row["id"])
        if par:
            fm["parent"] = _link_value(par["source"], db, reg, spec, row)
    if spec.contain_task:
        kids = [c["target"] for c in db.children_of(row["id"])]
        if kids:
            fm["subtask"] = _link_value(", ".join(kids), db, reg, spec, row)
    # marker comment (line 1, invisible) → title → metadata block; the head is
    # fenced by invisible `<!-- proj:head:start/end -->` comments: everything
    # between them is tool-owned (managed by `proj`, never hand-edited).
    head = _head_lines(fm)
    lines = [f"<!-- proj:{spec.record_type} {row['id']} -->",
             HEAD_START,
             f"# {row['id']}. {row['title']}", "",
             "\n".join(l + ("  " if i < len(head) - 1 else "")
                       for i, l in enumerate(head)) if head else "",
             ""]
    if row.get("one_sentence"):
        lines.append(f"_{row['one_sentence']}_")  # one-sentence summary, below
    lines += [HEAD_END, ""]
    limit = _logs_limit()
    if limit is not 0:
        logs = db.logs(row["id"], limit=limit)
        if logs:
            lines += ["| Date | Category | Change |", "|---|---|---|"]
            lines += [f"| {e['date']} | {e['category']} | {_log_change(e, spec)} |"
                      for e in logs]
            from proj.engine.views import status_legend  # lazy: views↔store cycle
            lines += status_legend(spec)
            lines.append("")
    sections = json.loads(row["sections"] or "{}")
    for sec in spec.sections:
        lines += [f"## {sec.name}", ""]
        if sec.kind == "task":
            # editable task tree block: tasks whose parent is this record
            # (kind=parent edge), recursively — hand-checkable, synced back
            from proj.engine.views import task_tree_lines  # lazy: views↔store cycle
            task_spec = reg.get("task")
            lines += (task_tree_lines(
                db.all(task_spec) if task_spec else [], row["id"], db)
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
            row = db.get(spec, u)
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
    """Extract (record_type, id) marker, title and {section_lower: raw} from an md.

    The tool-owned head (id marker comment + a blockquote of `> **key:** value`
    lines) sits ABOVE the title and never matches the title/section patterns, so
    the whole text is scanned directly.
    """
    m = MARK_RE.search(text)
    marker = (m.group(1), m.group(2).upper()) if m else (None, None)
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
    """Status from the record head (leading emoji stripped)."""
    s = str(fm_fields(text).get("status") or "")
    # drop a leading emoji: status_display renders "<emoji> <name>";
    # an emoji is always non-ASCII, a status name always ASCII
    parts = s.split(" ", 1)
    if len(parts) == 2 and not parts[0].isascii():
        return parts[1].strip()
    return s


# the head block + summary are fenced by invisible comments (tool-owned); the
# start fence may carry an instruction message after `start`
HEAD_START_RE = re.compile(r"^<!-- proj:head:start.*?-->[ \t]*$", re.MULTILINE)
HEAD_END_RE = re.compile(r"^<!-- proj:head:end -->[ \t]*$", re.MULTILINE)
# one-sentence summary: a single italic line just below the metadata block
SUMMARY_LINE_RE = re.compile(r"^_([^-_].*|[^_])_$")


def md_summary(text: str) -> str:
    """The one-sentence summary: the italic line inside the head fence (new
    layout); falls back to the old `> **summary:**` head row for files
    rendered before the fence existed (md wins either way)."""
    m_start, m_end = HEAD_START_RE.search(text), HEAD_END_RE.search(text)
    if m_start and m_end:
        for line in text[m_start.end():m_end.start()].splitlines():
            s = SUMMARY_LINE_RE.fullmatch(line.strip())
            if s:
                return s.group(1).strip()
        return ""
    fm = fm_fields(text)
    if "summary" in fm:
        v = str(fm["summary"]).strip()
        return "" if v == "—" else v  # the placeholder is not real content
    return ""


# ---- mutations ---------------------------------------------------------------

def new_record(cfg: ProjConfig, record_type: str | RecordTypeSpec, title: str,
               summary: str = "", commit: str = "",
               spec: RecordTypeSpec | None = None):
    if isinstance(record_type, RecordTypeSpec):
        spec = record_type
    else:
        reg = load_registry(cfg.root)
        spec = spec or reg[record_type]
    db = open_db(cfg)
    id_ = dbmod.next_id(spec, db)
    status = spec.status_allowed[0] if spec.status_allowed else ""
    created = date.today().isoformat()
    db.insert(spec, id_, title, status, created, summary, commit)
    db.add_log(id_, "created", category="Added", when=created)
    row = db.get(spec, id_)
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
    db.set(spec, row["id"], status=status)
    if old != status:
        db.add_log(row["id"], category="Changed", when=when,
                   from_status=old, to_status=status)
    fresh = db.get(spec, row["id"])
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
    db.add_log(row["id"], text, category or cat)
    fresh = db.get(spec, row["id"])
    p = file_path(cfg, spec, fresh)
    p.write_text(render_record(spec, fresh, db), encoding="utf-8")
    db.close()


def update_record(cfg: ProjConfig, record_type: str, id_: str, title: str | None = None,
                  summary: str | None = None, comment: str | None = None) -> dict:
    """Update a record's title / summary (and optionally append a changelog entry)."""
    spec, db = spec_and_db(cfg, record_type)
    row = by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        raise ValueError(f"{record_type} {id_} not found")
    old_p = file_path(cfg, spec, row)  # a title change also renames the md file
    if title is not None:
        if not title.strip():
            db.close()
            raise ValueError("title must be non-empty")
        db.set(spec, row["id"], title=title.strip())
        db.add_log(row["id"], f"title: {row['title']} → {title.strip()}", "Changed")
    if summary is not None:
        db.set(spec, row["id"], one_sentence=summary.strip())
        db.add_log(row["id"], "summary updated", "Changed")
    if comment:
        from proj.core.templates import load_changelog_spec  # lazy: avoid cycle
        cat, text = dbmod.parse_summary(comment, load_changelog_spec(cfg.root))
        db.add_log(row["id"], text, cat)
    fresh = db.get(spec, row["id"])
    p = file_path(cfg, spec, fresh)
    if p != old_p and old_p.is_file():
        old_p.rename(p)
    p.write_text(render_record(spec, fresh, db), encoding="utf-8")
    db.close()
    render_all(cfg)
    return fresh


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
    """Read one md into the DB. strict=True (sync): id marker required.
    Returns (problems, notes)."""
    text = p.read_text(encoding="utf-8")
    (mk, u), title, raw_sections = parse_record(text)
    if strict:
        if mk != spec.record_type or not u:
            return [f"{p.name}: missing/foreign id marker — run `proj adopt`"], []
        row = db.get(spec, u)
        if row is None:
            return [f"{p.name}: id {u} not in DB — run `proj adopt`"], []
        # a renamed filename (id in the marker != the file's id) is a decision
        # for the user, not something to do silently
        if not p.name.startswith(row["id"] + "-"):
            return [f"{p.name}: filename id does not match {row['id']} (renamed? "
                    f"edit the marker or move the file)"], []
        notes = []
        if title and title != row["title"]:
            notes.append(f"{p.name}: title drift in md (DB wins: {row['title']!r})")
        if md_status(text) not in ("", row["status"]):
            notes.append(f"{p.name}: status drift in md (DB wins: {row['status']!r})")
        sections = {s.name.lower(): normalize(raw_sections.get(s.name.lower(), ""))
                    for s in spec.sections if s.kind == "text"}
        db.set(spec, u, sections=json.dumps(sections, ensure_ascii=False))
        # one_sentence: md wins (hand-written result/progress), DB otherwise
        db.set(spec, u, one_sentence=md_summary(text) or (row.get("one_sentence") or ""))
        # record_type-specific ingest side effects (task: checkbox auto-promotion)
        from proj.engine.record import record_for  # lazy: record↔store cycle
        record_for(spec).on_ingest(db, row, sections, notes, p.name)
        return [], notes
    # adopt mode
    if mk == spec.record_type and u and db.get(spec, u):
        return [], []  # already in DB
    m = _id_prefix_re(spec).match(p.name)
    if not m:
        return [f"{p.name}: filename does not start with a valid id "
                f"for record_type {spec.record_type!r}"], []
    id_ = m.group(1).upper()
    if db.get(spec, id_):
        return [f"{p.name}: duplicate id {id_!r} in {spec.record_type} — resolve manually"], []
    if not title:
        return [f"{p.name}: no '# Title' heading — cannot adopt"], []
    # headings often repeat the id prefix ("# DEC-0001 Some Title") — strip it
    title = re.sub(rf"^{re.escape(id_)}[.\s]\s*", "", title)
    status = spec.status_allowed[0] if spec.status_allowed else ""
    created = date.today().isoformat()
    db.insert(spec, id_, title, status, created, md_summary(text))
    db.add_log(id_, "adopted into proj", category="Added", when=created)
    sections = {s.name.lower(): normalize(raw_sections.get(s.name.lower(), ""))
                for s in spec.sections if s.kind == "text"}
    db.set(spec, id_, sections=json.dumps(sections, ensure_ascii=False))
    # adopt: same record_type hook, fresh row; promotion notes are dropped
    from proj.engine.record import record_for  # lazy: record↔store cycle
    record_for(spec).on_ingest(db, db.get(spec, id_),
                               sections, [], p.name)
    # canonical filename is id + slugify(title); if the user's file used a
    # different slug, move it so only the rendered name remains
    canonical = cfg.root / spec.dir / (id_ + "-" + slugify(title) + ".md")
    if p != canonical and not canonical.exists():
        canonical.parent.mkdir(parents=True, exist_ok=True)
        p.rename(canonical)
    return [], [f"{p.name}: adopted as {spec.record_type} {id_}"]


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
    """Absorb existing md files (id marker if present; otherwise id check).
    Then render everything. Returns (problems, notes)."""
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
Ops cheatsheet (RT = a record_type — name or code, case-insensitive; VERB = one from its template):
  new:        proj RT new TITLE [--field name=value] [--parent N] [-s N]
  change:     proj RT <VERB> ID [-t X] [-s X] [-c "…"]  (state machine + optional
                                                         title/summary/comment update)
  edit:       proj RT update ID [-t X] [-s X] [-c "…"]  (update title / summary /
                                                         append a changelog entry)
  md edited:  proj sync | proj adopt (first time, unmanaged files)
  query:      proj RT list | proj RT show ID [--history]
  overview:   proj info [--json]"""


def info(cfg: ProjConfig) -> dict:
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    record_types = {}
    for record_type, spec in reg.items():
        rows = db.all(spec)
        if not rows:
            continue  # the unified records table always exists; an empty type is skipped
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        num = f"{spec.numbered}"  # D9: numbered is always an int
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
