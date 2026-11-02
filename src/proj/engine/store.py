"""Record storage engine (module-level functions only): YAML template (schema) +
SQLite (storage) + md (input channel). The *behavior* layer (Record class
hierarchy, record_for) lives in record.py — this module calls back lazily.

md file shape (rendered by the engine):

    <!-- proj:decision DEC-0001 -->        <- line 1: tool-owned id marker (a comment,
                                             <-   invisible in preview)
    <!-- proj:head:start … TOOL-MANAGED … -->  <- head fence (HEAD_START): everything
    # 0001. Title                          <-   between start and end is maintained by
                                             <-   `proj` (title + metadata block + summary
                                             <-   + the changelog); the start line says:
                                             <-   never hand-edit
    > **status:** 📛 Deprecated             <- tool-owned metadata block (a blockquote,
    > **supersedes:** [0001](0001-x.md)    <-   not front matter, so md validators
    > **subtask:** B                        <-   leave it alone; renders as a gray
    > **created/done:** 2026-10-29 → …     <-   sidebar, every line but the last
                                             <-   hard-wrapped; same-type id refs
                                             <-   link
    _one sentence summary_                 <- optional italic line below the block
                                             <-   (shown only when set; md wins)
    <details><summary>Changelogs</summary>  <- the changelog is INSIDE the head fence
    - `2026-10-29 - status 🌱 → ✅ Accepted` <-   (tool-owned; collapsed by default;
    …legend…                            <-   latest PROJ_LOGS rows; 0=none, -1=all)
    </details>
    <!-- proj:head:end -->                 <- head fence end (HEAD_END): everything
                                             <-   above it (block + summary + changelog)
                                             <-   is owned by `proj`, never hand-edit

    <details><summary>Task list</summary>  <- the attached-task tree, right below the
    - [x] T-000001 Title                  <-   head fence and BEFORE the user-owned
    </details>                            <-   sections; rendered for ANY record that
                                         <-   parents tasks (kind=parent edges),
                                         <-   collapsed by default

    ## Context                             <- user-owned content (the only
    ...                                    <- part humans/LLMs edit)

The metadata block is a blockquote of `> **key:** value` lines — NOT a front
matter, so md frontmatter validators leave it alone. Every line but the last
ends with two spaces (markdown hard wrap — a hard wrap on the last line would
pull the following content into the blockquote). Record id refs (supersedes /
superseded_by / related / a task's parent) render as `[filename](path)` links:
the text is the target's full filename, the path relative to this record's own
file (same dir → bare filename, across dirs → ../dir/filename); one ref = one
line, several = a bare key line + `> - item` list. The attached-task tree
(children via the kind=parent relation, any depth, fully expanded) renders
right below the head fence for ANY record that parents tasks. The id marker
comment sits
on line 1, above the block.

sync: md → DB (sections win) → render all md (tool-owned parts win).
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from proj.core import db as dbmod
from proj.core import fsio
from proj.core.config import ProjConfig
from proj.core.db import ProjError
from proj.core.templates import RecordTypeSpec, load_registry, slugify

# ---- S.2: the project write lock -----------------------------------------------
# Every write command holds `write_lock` for its whole run — at the CLI entry
# points (cli.py), the MCP write tools (mcp.py) and the engine entry points
# below (ensure_synced / sync / adopt) — so concurrent writers queue up
# (fsio.LOCK_TIMEOUT); read commands take none. Re-entrant per thread (the CLI
# entry lock and the engine lock nest in the same thread — the re-entrancy lives
# in fsio.project_lock itself).

@contextmanager
def write_lock(cfg: ProjConfig, cmd: str = ""):
    """The project write lock for this command (fsio.project_lock, re-entrant)."""
    with fsio.project_lock(cfg.root, cmd):
        yield

# tool-owned id marker (an html-comment line, on line 1 above the blockquote head)
# id shape: 1-3 letters + '-' + a fixed-width DIGIT suffix, letters+digits == 7
# (8 chars total), e.g. DEC-0001, T-000001. The suffix is digits ONLY — there is
# no letter overflow (see db.next_id; a record id is {code}-{digits} until the
# all-digit space is used up, then new ids raise). Case-insensitive, not glued to
# other alnums, uppercased on use. MARK_RE wraps this shape in the line-1 comment.
_ID_SHAPE = (
    r"(?<![0-9A-Za-z])([A-Za-z]-[0-9]{6}|[A-Za-z]{2}-[0-9]{5}"
    r"|[A-Za-z]{3}-[0-9]{4})(?![0-9A-Za-z])"
)
MARK_RE = re.compile(rf"^<!-- proj:([\w-]+) {_ID_SHAPE} -->[ \t]*$", re.MULTILINE)
TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
# one head line: `> **key:** value` (+ trailing two spaces = markdown hard wrap)
HEAD_LINE_RE = re.compile(r"^> \*\*(\w+):\*\*\s*(.*)$")
# head fence: an invisible comment pair marks the tool-owned head region
# (id marker above, title + metadata block + optional summary inside). The
# start line carries a human/agent-readable instruction that the region is
# maintained by `proj` and must not be hand-edited (edit via `proj` instead).
HEAD_START = "<!-- proj:head:start — TOOL-MANAGED: metadata, summary and " \
             "changelog are maintained by `proj`; never hand-edit this region " \
             "(use `proj`) -->"
HEAD_END = "<!-- proj:head:end -->"


def open_db(cfg: ProjConfig) -> dbmod.Db:
    return dbmod.Db(cfg.db_path)


def spec_of(cfg: ProjConfig, record_type: str) -> RecordTypeSpec:
    """The record_type's spec (validated to be registered) — no connection opened.
    R3: write functions resolve the spec, then hold ONE connection themselves."""
    reg = load_registry(cfg.root)
    if record_type not in reg:
        raise ValueError(f"unknown record_type {record_type!r} "
                         f"(registered: {', '.join(sorted(reg))})")
    return reg[record_type]


def spec_and_db(cfg: ProjConfig, record_type: str):
    return spec_of(cfg, record_type), open_db(cfg)


# a bare "sequence" ref: pure digits, optionally zero-shortened ("1"/"0001").
_BARE_SEQ_RE = re.compile(r"0*\d+")


def _canonical_ref(spec: RecordTypeSpec, ref: str) -> str | None:
    """The canonical id for a ref of this type (None if the type isn't numbered
    or the ref doesn't match). Accepts the full id (any width), a code-prefixed
    shortened suffix ("T-1"/"T-0001" → "T-000001", "DEC-1" → "DEC-0001"), or a
    bare suffix ("1", "0001" → the type's own code, e.g. DEC-0001)."""
    if not isinstance(spec.numbered, int) or not ref:
        return None
    seq = ref
    if "-" in ref:
        head, _, tail = ref.partition("-")
        if head.upper() != spec.code:
            return None
        seq = tail
    if not _BARE_SEQ_RE.fullmatch(seq):
        return None
    n = int(seq)
    if n > 10 ** int(spec.numbered):  # out of the all-digit id space
        return None
    return f"{spec.code}-{n:0{spec.numbered}d}"


def by_id(cfg: ProjConfig, spec: RecordTypeSpec, db: dbmod.Db, id_: str) -> dict | None:
    row = db.get(spec, id_)
    if row is not None:
        return row
    # tolerant: a bare number ("1"/"0001") → the canonical {code}-{n:0w}
    cand = _canonical_ref(spec, id_)
    return db.get(spec, cand) if cand else None


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
        for spec in reg.values():
            cand = _canonical_ref(spec, ref)
            if cand and cand != ref.upper() and db.get(spec, cand) is not None:
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
    # a shortened/bare ref may fit SEVERAL types — collect every match first;
    # an ambiguous one raises (use the canonical id), never "first type wins"
    matches: list[tuple] = []
    for spec in reg.values():
        cand = _canonical_ref(spec, ref)
        if cand and cand != ref.upper():
            r = db.get(spec, cand)
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
        pre, n = r"[A-Za-z]{1,3}-", r"[0-9A-Za-z]{4,6}"
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
    One id → a single string; several → a list (the head inlines it on the
    key line). Resolves across any record_type (parent→plan)."""
    root = Path(db.path).parent
    src_file = file_path(ProjConfig(root), spec, row)
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
    # UTC ISO8601 timestamps compare chronologically as strings; the head row
    # shows the date part (D10)
    return dbmod.show_date(min(dates)) if dates else ""


def _logs_limit() -> int | None:
    """PROJ_LOGS: how many changelog rows to render per record md.
    0 = none, -1 = all, N = latest N; unset/empty = 5 (the old default)."""
    raw = (os.environ.get("PROJ_LOGS") or "").strip()
    if not raw:
        return 5
    n = int(raw)
    return None if n < 0 else n


def _log_body(e: dict, spec: RecordTypeSpec) -> str:
    """The text of a changelog entry: the message when one is set (the record's
    status at that moment only as its emoji prefix), else the status AFTER the
    event (emoji + name, i.e. `status_display`)."""
    s = e["status"] or ""
    if e["message"]:
        return f"{spec.emojify(s)} {e['message']}"
    return spec.status_display(s)


def _log_entry(e: dict, spec: RecordTypeSpec) -> str:
    """One changelog line: a backticked ``- DATE - <body>`` (body per
    `_log_body`), S.4: `· <actor>` appended when the row records one (rows
    written before S.4 have no actor and render as before)."""
    line = f"- `{dbmod.show_date(e['date'])} - {_log_body(e, spec)}`"
    if e.get("actor"):
        line += f" · {e['actor']}"
    return line


def _head_lines(fm: dict) -> list[str]:
    """Tool-owned head lines: a blockquote of `> **key:** value` (a gray
    sidebar), every line ending in a markdown hard break (two trailing
    spaces). A list value renders INLINED on the key line (`,`-separated —
    no per-item rows), e.g. `> **subtask:** [T-…](…md), [T-…](…md)`."""
    lines: list[str] = []
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"> **{k}:** " + ", ".join(_head_value(x) for x in v))
        else:
            lines.append(f"> **{k}:** {_head_value(v)}")
    return lines


def render_record(spec: RecordTypeSpec, row: dict, db: dbmod.Db,
                  registry: dict[str, RecordTypeSpec] | None = None,
                  task_rows: list[dict] | None = None) -> str:
    # R7: bulk renderers pre-fetch the task rows once and pass them in; a
    # single-record caller leaves it None (one db.all, fine).
    # tool-owned head: id marker comment (line 1) + blockquote metadata
    # (status/created/done rows + relation rows, id refs as links to the
    # target's full filename); the one-sentence summary is an italic line
    # below the block (shown only when set — never a "Summary:" label).
    # The attached-task tree (direct children via relations, at most 2
    # levels) renders for ANY record that parents tasks, right below the
    # head fence (replaces the old contain_task gate + subtask head row).
    reg = registry if registry is not None else load_registry(Path(db.path).parent)
    fm = {
        "status": f"`{spec.status_display(row['status'])}`",
        # D10: stored UTC timestamps, shown as the date part; one combined
        # created/done row ("created → in progress" until done, then
        # "created → done date")
        "created/done": f"{dbmod.show_date(row.get('created') or '') or '—'} → "
                        f"{_done_date(spec, row, db) or 'in progress'}",
    }
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
        # K4: the records whose checklist items reference this task (the `picked
        # by` reverse of the `→ T-…` reference; shown only when non-empty)
        picked = db.picked_by(row["id"])
        if picked:
            fm["picked by"] = _link_value(", ".join(picked), db, reg, spec, row)
    # marker comment (line 1, invisible) → title (editable; synced back to the
    # DB) → the head fence: the TOOL-OWNED region is blockquote + summary +
    # the changelog — everything between start and end is maintained by `proj`
    # (the fence end sits AFTER the changelog, never hand-edit it).
    head = _head_lines(fm)
    lines = [f"<!-- proj:{spec.record_type} {row['id']} -->",
             f"# {row['id']}. {row['title']}", "",
             HEAD_START,
             # every blockquote line ends with a markdown hard break (two
             # trailing spaces) so each row stays on its own line in any renderer
             "\n".join(line + "  " for line in head) if head else "",
             ""]
    if row.get("summary"):
        lines.append(f"_{row['summary']}_")  # one-sentence summary, inside
    limit = _logs_limit()
    if limit != 0:
        logs = db.logs(row["id"], limit=limit)  # newest first
        if logs:
            # the changelog is inside the tool-owned head region: collapsed by
            # default; the status legend (emoji → name) sits inside, at the
            # bottom, so the emoji in each line stays decodable
            lines += ["<details>", "<summary>Changelogs</summary>", ""]
            lines += [_log_entry(e, spec) for e in logs]
            legend = " · ".join(spec.status_display(s) for s in spec.status_allowed)
            lines += ["", legend, "</details>"]
    lines += ["", HEAD_END]
    sections = json.loads(row["sections"] or "{}")
    # the Tasks block sits right BELOW the head fence, before the user-owned
    # sections, for ANY record that parents tasks (kind=parent edges). Content
    # = the record's task tree from the DB (A2: generated, never stored).
    # A2/Q5: the block is generated ENTIRELY from the DB (the relations table) —
    # the record's task tree, indented by depth, each line with the task's
    # one-line summary. Nothing task-related is stored in the md; the task table
    # is fetched ONCE per render_all.
    from proj.engine.views import render_tasks_block  # lazy: cycle
    task_spec = reg.get("task")
    if task_rows is None and task_spec is not None:
        task_rows = db.all(task_spec)
    block = render_tasks_block(
        task_rows, db, task_spec, reg,
        Path(db.path).parent / spec.dir,
        Path(db.path).parent / task_spec.dir, row["id"])
    if block:
        lines += ["", "<details open>", "<summary>Tasks</summary>", ""]
        lines += [*block, "</details>", ""]
    from proj.engine import items as items_mod  # lazy: store↔items cycle
    item_specs = {it.section: it for it in spec.items}
    for sec in spec.sections:
        lines += [f"## {sec.name}", ""]
        it = item_specs.get(sec.name)
        if it is not None:
            # a checklist section renders entirely from the DB state (its intro
            # lives in the sections JSON alongside the item lines)
            state = items_mod.load(db, row["id"], it)
            state.intro = items_mod._intro_of(it, row)
            lines.append(items_mod.render_section(state, it) or "")
        else:
            lines.append(sections.get(sec.name.lower(), "").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _md_drifts(p: Path, row: dict, spec: RecordTypeSpec, db) -> bool:
    """One record file vs the DB (its text sections, and the items section when
    the record_type has items)."""
    _mk, _u, raw = parse_record(p.read_text(encoding="utf-8"))
    stored = json.loads(row["sections"] or "{}")
    from proj.engine import items as items_mod  # lazy: store↔items cycle
    item_sec = {it.section.lower() for it in spec.items}
    for it in spec.items:  # each checklist section, its own drift
        if items_mod.drift(db, it, row, raw.get(it.section.lower(), "")):
            return True
    for s in spec.sections:
        # the checklist sections are judged above via items_mod.drift — their
        # raw list text never matches the stored section, so skip them there
        if s.kind == "text" and s.name.lower() not in item_sec and \
                normalize(raw.get(s.name.lower(), "")) != stored.get(s.name.lower(), ""):
            return True
    return False


def md_drift(cfg: ProjConfig) -> list[str]:
    """md files whose section content differs from the DB (unsynced edits)."""
    reg = load_registry(cfg.root)
    db = open_db(cfg)
    try:
        out: list[str] = []
        for spec in reg.values():
            for p in _scan_files(cfg, spec, None):
                (mk, u), _t, _raw = parse_record(p.read_text(encoding="utf-8"))
                if mk != spec.record_type or not u:
                    continue
                row = db.get(spec, u)
                if row is not None and _md_drifts(p, row, spec, db):
                    out.append(p.name)
        return out
    finally:
        db.close()


def ensure_synced(cfg: ProjConfig) -> None:
    """A1 (Q4): before a write command overwrites record files, ingest every
    file with unsynced hand edits (md → DB). No-op when nothing drifted.
    Problems abort the whole operation — nothing is rendered or half-written.
    S.2: holds the project write lock (re-entrant when nested under a
    write command that already holds it)."""
    with write_lock(cfg, "sync"):
        reg = load_registry(cfg.root)
        # R3: one write transaction — the hand edits are ingested atomically (or
        # the whole ingest rolls back when a file is malformed)
        with open_db(cfg) as db:
            db.begin()  # R3: one write transaction for the whole command
            for spec in reg.values():
                for p in _scan_files(cfg, spec, None):
                    (mk, u), _t, _raw = parse_record(p.read_text(encoding="utf-8"))
                    if mk != spec.record_type or not u:
                        continue
                    row = db.get(spec, u)
                    if row is None or not _md_drifts(p, row, spec, db):
                        continue
                    problems, _ = _ingest_md(cfg, spec, db, p, strict=True)
                    if problems:
                        raise ProjError("unsynced md edits with problems — fix and retry:\n"
                                        + "\n".join(f"  {x}" for x in problems))


def render_all(cfg: ProjConfig, db: dbmod.Db | None = None) -> list[Path]:
    """Phase 2: DB → every record file + global views. Returns written paths.
    B1: writes only files whose rendered content actually changed (a command no
    longer rewrites every file). No auto-sync here: render_all always runs AFTER
    a write, when the md is stale by design — A1's auto-sync lives at the write
    command entry points (ensure_synced), not in the render itself.
    R3: when called from inside a write command, pass that command's connection
    (render reads its UNCOMMITTED writes — a fresh connection would not see them
    under WAL); a standalone render opens and closes its own read connection."""
    from proj.engine.views import render_changelog_md
    from proj.engine.wip import render_wip_md  # WIP.md replaces TASKLIST.md (W1)
    reg = load_registry(cfg.root)
    own_db = db is None
    db = open_db(cfg) if own_db else db
    try:
        written: list[Path] = []
        # R7: the Tasks block needs the task rows for EVERY record — fetch the
        # task table ONCE here (otherwise render_record re-reads it per record,
        # O(N^2) db.all) and hand it down.
        task_rows = db.all(reg["task"]) if "task" in reg else []
        for spec in reg.values():
            d = cfg.root / spec.dir
            d.mkdir(parents=True, exist_ok=True)
            for row in db.all(spec):
                p = file_path(cfg, spec, row)
                text = render_record(spec, row, db, reg, task_rows=task_rows)
                if p.exists() and p.read_text(encoding="utf-8") == text:
                    continue  # unchanged (B1)
                fsio.write_atomic(p, text)  # S.2: atomic (no half-written file)
                written.append(p)
        for view in (render_wip_md(cfg, db, reg), render_changelog_md(cfg, db)):
            if view is None:
                continue
            if view.path.exists() and view.path.read_text(encoding="utf-8") == view.text:
                continue  # unchanged (B1)
            fsio.write_atomic(view.path, view.text)  # S.2
            written.append(view.path)
        return written
    finally:
        if own_db:
            db.close()


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
    """Status from the record head (backticks + leading emoji stripped)."""
    s = str(fm_fields(text).get("status") or "").strip("`").strip()
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
    """The one-sentence summary: the italic line inside the head fence; falls
    back to the `> **summary:**` head row for files without a fence (md
    wins either way)."""
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
    ensure_synced(cfg)  # A1: an unsynced hand edit is ingested, never clobbered
    # R3: one write transaction — insert + created-log + the rendered md file
    with open_db(cfg) as db:
        db.begin()  # R3: one write transaction for the whole command
        id_ = dbmod.next_id(spec, db)
        status = spec.status_allowed[0] if spec.status_allowed else ""
        created = dbmod.utc_now()  # D10: UTC timestamp (rendered as the date part)
        db.insert(spec, id_, title, status, created, summary, commit)
        db.add_log(id_, "created", category="Added", when=created)
        row = db.get(spec, id_)
        p = file_path(cfg, spec, row)
        p.parent.mkdir(parents=True, exist_ok=True)
        fsio.write_atomic(p, render_record(spec, row, db))  # S.2: atomic
    render_all(cfg)  # keep views (WIP.md etc.) in sync with the new record
    return row, p


def status_set(cfg: ProjConfig, record_type: str, id_: str, status: str,
               when: str | None = None) -> dict:
    ensure_synced(cfg)  # A1
    spec = spec_of(cfg, record_type)
    # R3: one write transaction — the status set + changelog + the re-rendered md
    with open_db(cfg) as db:
        db.begin()  # R3: one write transaction for the whole command
        row = by_id(cfg, spec, db, id_)
        if row is None:
            raise ValueError(f"{record_type} {id_} not found")
        if not any(re.fullmatch(a.format(id=row["id"]), status) for a in spec.status_allowed):
            raise ValueError(f"status {status!r} not allowed for {record_type} "
                             f"(allowed: {', '.join(spec.status_allowed)})")
        old = row["status"]
        db.set(spec, row["id"], status=status)
        if old != status:
            db.add_log(row["id"], category="Changed", when=when)  # row = the new status
            # WIP (W3/W6): a switch into a wip.enter status auto-adds to WIP.md,
            # a switch into a terminal status auto-returns (writing WIP.md's
            # edited item section back first); manual return is never re-triggered
            # by a same-status op — only a real switch triggers here.
            from proj.engine import wip as wipmod  # lazy: store↔wip cycle
            wipmod.auto_status_wip(db, spec, row["id"], old, status)
        fresh = db.get(spec, row["id"])
        p = file_path(cfg, spec, fresh)
        fsio.write_atomic(p, render_record(spec, fresh, db))  # S.2: atomic
    render_all(cfg)  # status changes affect rendered views
    return fresh


def add_log(cfg: ProjConfig, record_type: str, id_: str, summary: str,
            category: str | None = None) -> None:
    spec = spec_of(cfg, record_type)
    from proj.core.templates import load_changelog_spec  # lazy: avoid cycle
    cat, text = dbmod.parse_summary(summary, load_changelog_spec(cfg.root))
    # R3: one write transaction — the changelog entry + the re-rendered md
    with open_db(cfg) as db:
        db.begin()  # R3: one write transaction for the whole command
        row = by_id(cfg, spec, db, id_)
        if row is None:
            raise ValueError(f"{record_type} {id_} not found")
        db.add_log(row["id"], text, category or cat)
        fresh = db.get(spec, row["id"])
        p = file_path(cfg, spec, fresh)
        fsio.write_atomic(p, render_record(spec, fresh, db))  # S.2: atomic


def update_record(cfg: ProjConfig, record_type: str, id_: str, title: str | None = None,
                  summary: str | None = None, message: str | None = None) -> dict:
    """Update a record's title / summary (and optionally append a changelog entry)."""
    ensure_synced(cfg)  # A1
    spec = spec_of(cfg, record_type)
    # R3: one write transaction — the update(s) + changelogs + the re-rendered md
    with open_db(cfg) as db:
        db.begin()  # R3: one write transaction for the whole command
        row = by_id(cfg, spec, db, id_)
        if row is None:
            raise ValueError(f"{record_type} {id_} not found")
        old_p = file_path(cfg, spec, row)  # a title change also renames the md file
        if title is not None:
            if not title.strip():
                raise ValueError("title must be non-empty")
            db.set(spec, row["id"], title=title.strip())
            db.add_log(row["id"], f"title: {row['title']} → {title.strip()}", "Changed")
        if summary is not None:
            db.set(spec, row["id"], summary=summary.strip())
            db.add_log(row["id"], "summary updated", "Changed")
        if message:
            from proj.core.templates import load_changelog_spec  # lazy: avoid cycle
            cat, text = dbmod.parse_summary(message, load_changelog_spec(cfg.root))
            db.add_log(row["id"], text, cat)
        fresh = db.get(spec, row["id"])
        p = file_path(cfg, spec, fresh)
        if p != old_p and old_p.is_file():
            old_p.rename(p)
        fsio.write_atomic(p, render_record(spec, fresh, db))  # S.2: atomic
    render_all(cfg)
    return fresh


# ---- sync / adopt --------------------------------------------------------------

def _ingest_md(cfg: ProjConfig, spec: RecordTypeSpec, db: dbmod.Db, p: Path,
               strict: bool, skip_items: bool = False) -> tuple[list[str], list[str]]:
    """Read one md into the DB. strict=True (sync): id marker required.
    skip_items: leave the record's checklist alone (WIP.md just wrote it back —
    the record md's stale list must not clobber it). Returns (problems, notes)."""
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
        # the title is user-owned (md wins): a changed title goes back to the
        # DB and the file is renamed to the new slug (the id never changes)
        if title and title != row["title"]:
            db.set(spec, u, title=title)
            old_p, p = p, file_path(cfg, spec, db.get(spec, u))
            if p != old_p:
                if p.exists():
                    return [f"{p.name}: title {title!r} renames to an existing "
                            f"file {p.name} — resolve manually"], []
                old_p.rename(p)
                notes.append(f"{old_p.name}: title {title!r} synced, renamed to {p.name}")
        if md_status(text) not in ("", row["status"]):
            notes.append(f"{p.name}: status drift in md (DB wins: {row['status']!r})")
        sections = {s.name.lower(): normalize(raw_sections.get(s.name.lower(), ""))
                    for s in spec.sections if s.kind == "text"}
        # the Tasks block is tool-owned (A2): generated from the DB, never
        # stored — hand lines in it are dropped on the next re-render (the
        # D11 gate still refuses foreign task links during the sync itself)
        # item checklists (items.py): the items section's LIST is owned by the DB —
        # only its intro (text before the list) is stored in the section. Validate
        # fully before any write; the items write + changelog + promote follow.
        # each checklist section's LIST is DB-owned — only its intro is stored;
        # the checklists themselves sync below (their own intro + changelog +
        # promote), so drop them from the stored-sections rewrite here.
        from proj.engine import items as items_mod  # lazy: store↔items cycle
        for it in spec.items:
            sections.pop(it.section.lower(), None)
        db.set(spec, u, sections=json.dumps(sections, ensure_ascii=False))
        # the checklists: sync last (after the text sections) — each writes its
        # own intro into the sections JSON; a problem leaves everything untouched.
        # skip_items: WIP.md just wrote this record's WIP checklist back — that
        # one (its `wip: true`) must not be re-synced from the (possibly stale)
        # record md; the other checklists sync as usual.
        from proj.engine import item_ops as ops_mod  # lazy: store↔item_ops
        for it in spec.items:
            if skip_items and it.wip:
                # preserve the intro the WIP write-back stored under
                # `<section>/intro` (this sections rewrite must not clobber it)
                intro = (json.loads(row.get("sections") or "{}")
                         .get(items_mod._intro_key(it.section), ""))
                if intro:
                    sections[items_mod._intro_key(it.section)] = intro
                db.set(spec, u, sections=json.dumps(sections, ensure_ascii=False))
                continue
            # S.3: a hand-edited owner is a deliberate hand-over — accepted, but
            # named in the notes (`item #A01 owner alex → billy`)
            notes += ops_mod.sync_owner_notes(
                db, spec, db.get(spec, u),
                raw_sections.get(it.section.lower(), ""))
            problems, n = items_mod.sync_section(
                db, spec, db.get(spec, u),
                raw_sections.get(it.section.lower(), ""),
                source=p.name, it=it)
            if problems:
                return problems, []
            notes += n
        # summary: md wins (hand-written result/progress), DB otherwise
        db.set(spec, u, summary=md_summary(text) or (row.get("summary") or ""))
        # record_type-specific ingest side effects (base: no-op)
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
    created = dbmod.utc_now()  # D10: UTC timestamp (rendered as the date part)
    db.insert(spec, id_, title, status, created, md_summary(text))
    db.add_log(id_, "adopted into proj", category="Added", when=created)
    sections = {s.name.lower(): normalize(raw_sections.get(s.name.lower(), ""))
                for s in spec.sections if s.kind == "text"}
    # adopted md: the items section is parsed the same way — every line becomes
    # a NEW item (items.adopt); the intro stays in the stored section
    notes: list[str] = []
    if spec.items:
        from proj.engine import items as items_mod  # lazy: store↔items cycle
        for it in spec.items:
            sections.pop(it.section.lower(), None)
            # adopted md: every line becomes a NEW item — the record is fresh, so
            # no number can refer to it (adopt=True clears the numbers)
            problems, n = items_mod.sync_section(
                db, spec, db.get(spec, id_),
                raw_sections.get(it.section.lower(), ""),
                source=p.name, adopt=True, it=it)
            if problems:
                return problems, []
            notes += n
    db.set(spec, id_, sections=json.dumps(sections, ensure_ascii=False))
    # adopt: same record_type hook, fresh row; promotion notes are dropped
    from proj.engine.record import record_for  # lazy: record↔store cycle
    record_for(spec).on_ingest(db, db.get(spec, id_),
                               sections, [], p.name)
    # canonical filename (file_path); if the user's file used a different
    # slug, move it so only the rendered name remains
    canonical = file_path(cfg, spec, {"id": id_, "title": title})
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
    """md → DB (validated, normalized) → render all md. Returns (problems, notes).

    WIP (W5): WIP.md is the second edit entry for a record's checklist. Before
    anything is written, the scan decides per WIP entry: both the record md and
    WIP.md drifted → conflict (kept as is, the user keeps one side); only WIP.md
    drifted → its item section is written back into the record (the record md's
    items section is then skipped in the ingest, the stored intro preserved);
    otherwise the record md wins and WIP.md re-renders from the DB.
    S.2: the whole command (scan + write-back + ingest + render) holds
    # the project write lock."""
    with write_lock(cfg, "sync"):
        reg = load_registry(cfg.root)
        if files:
            return _sync_files(cfg, reg, files)
        from proj.engine import wip as wipmod  # lazy: store↔wip cycle
        with open_db(cfg) as db:  # read-only scan, its own short connection
            wip_only, conflicts = wipmod.wip_sync_plan(db, reg, cfg)
        problems = [f"{c}: item section edited in BOTH the record md and WIP.md — "
                    f"keep one side (edit only one, then sync)"
                    for c in conflicts]
        if problems:
            return problems, []
        if not wip_only:
            return _sync_files(cfg, reg, None)  # nothing to write back: the usual path
        notes = []
        # one write transaction: write back the WIP-edited checklists, then ingest the
        # record mds (skipping the items section of the just-written-back records, so
        # the stale md list can't clobber what WIP.md just stored)
        with open_db(cfg) as db:
            db.begin()  # R3: one write transaction for the whole command
            for rid, section in wip_only.items():
                row = dict(db.conn.execute(
                    "SELECT * FROM records WHERE id=?", (rid,)).fetchone())
                spec = reg[row["type"]]
                from proj.engine import item_ops as ops_mod  # lazy: store↔item_ops
                from proj.engine import items as items_mod  # lazy: store↔items cycle
                it = spec.wip_items  # WIP.md only ever writes back the wip checklist
                if it is None:
                    continue
                # S.3: a hand-edited owner in WIP.md is a hand-over too (a note)
                notes += [f"WIP.md: {n}" for n in
                          ops_mod.sync_owner_notes(db, it, row, section)]
                pr, nt = items_mod.sync_section(db, spec, row, section, source="WIP.md", it=it)
                if pr:
                    return pr, []
                notes += [f"WIP.md: {n}" for n in nt]
            for spec in reg.values():
                for p in _scan_files(cfg, spec, None):
                    (mk, u), _t, _raw = parse_record(p.read_text(encoding="utf-8"))
                    skip = (u in wip_only if mk == spec.record_type else False)
                    pr, nt = _ingest_md(cfg, spec, db, p, strict=True, skip_items=skip)
                    problems += pr
                    notes += nt
            if not problems:
                render_all(cfg, db=db)
        return problems, notes
def _sync_files(cfg: ProjConfig, reg: dict,
               files: list[Path] | None) -> tuple[list[str], list[str]]:
    """`proj sync` (all files) and `proj sync FILES…` — the record md → DB pass.
    No WIP write-back: this is the path sync reaches when WIP.md has no pending
    edits (or files are given explicitly, which never spans WIP.md)."""
    problems, notes = [], []
    with open_db(cfg) as db:
        db.begin()  # R3: one write transaction for the whole command
        for spec in reg.values():
            for p in _scan_files(cfg, spec, files):
                pr, nt = _ingest_md(cfg, spec, db, p, strict=True)
                problems += pr
                notes += nt
        if not problems:
            render_all(cfg, db=db)
    return problems, notes



def adopt(cfg: ProjConfig, files: list[Path] | None = None):
    """Absorb existing md files (id marker if present; otherwise id check).
    Then render everything. Returns (problems, notes).
    S.2: the whole command holds the project write lock."""
    with write_lock(cfg, "adopt"):
        reg = load_registry(cfg.root)
        problems, notes = [], []
        # R3: one write transaction — adopt every file, then re-render the views
        with open_db(cfg) as db:
            db.begin()  # R3: one write transaction for the whole command
            for spec in reg.values():
                for p in _scan_files(cfg, spec, files):
                    pr, nt = _ingest_md(cfg, spec, db, p, strict=False)
                    problems += pr
                    notes += nt
            if not problems:
                render_all(cfg, db=db)
        return problems, notes


# ---- info ----------------------------------------------------------------------

CHEATSHEET = """\
Ops cheatsheet (RT = a type — name or code, case-insensitive; VERB = one from its template):
  new:        proj RT new TITLE [--parent N] [-r ID…] [--supersede ID…] [--commit C]
              [-s "one-line summary"] [-y]
  change:     proj RT <VERB> ID [-t X] [-s X] [-m "…"]  (state machine + optional
                                                         title/summary/message update)
  edit:       proj RT update ID [-t X] [-s X] [-m "…"]  (update title / summary /
                                                         append a changelog entry)
  md edited:  proj sync | proj adopt (first time, unmanaged files)
  query:      proj RT ls [--status S] [--json] | proj RT show ID [--history] [--json]
  overview:   proj info [--json]"""


def _bug_count(cfg: ProjConfig):
    """Number of `.proj.bugs` records, or None when the file does not exist (B).
    DB-free (a file read) — safe even with a broken database."""
    from proj import bugs  # lazy

    n = bugs.count(cfg.root)
    return n if n else None


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
    views = ["WIP.md", "CHANGELOG.md"]
    out = {
        "root": str(cfg.root),
        "journal_mode": "DELETE (rollback journal — network filesystem)" if db.network_fs
        else "WAL",
        "record_types": record_types,
        "changelog": {"entries": len(cl), "versions": versions,
                      "latest": versions[-1] if versions else None},
        "views": views,
        "bug_reports": _bug_count(cfg),  # B: .proj.bugs (DB-free; None = file absent)
        "cheatsheet": CHEATSHEET.splitlines(),
    }
    db.close()
    return out


def info_text(d: dict) -> str:
    lines = [f"Project: {d['root']} — managed by proj (.proj.sqlite)", ""]
    if d.get("journal_mode", "WAL").startswith("DELETE"):
        lines.append("Journal: DELETE (rollback journal — the project is on a network "
                     "filesystem; avoid several machines writing it at once)")
    else:
        lines.append("Journal: WAL (local filesystem)")
    for rt, k in d["record_types"].items():
        lines.append(f"  {rt:<11} {k['count']:<3} record(s)  @ {k['dir']}/  [{k['numbered']}]")
        if k["statuses"]:
            lines.append("              " + ", ".join(
                f"{s} {n}" for s, n in sorted(k["statuses"].items())))
    cl = d["changelog"]
    if cl["entries"]:
        lines.append(f"Changelog: {cl['entries']} entries, latest {cl['latest']}")
    if d.get("bug_reports"):
        lines.append(f"Bug reports: {d['bug_reports']} (.proj.bugs)")
    lines.append("Views (rendered, never hand-edit): " + ", ".join(d["views"]))
    lines.append("")
    lines.append(CHEATSHEET)
    return "\n".join(lines) + "\n"
