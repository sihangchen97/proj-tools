"""Rendered views (generated — never hand-edited): TASKLIST.md, CHANGELOG.md,
and per-record view sections (e.g. plan's Task List)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from proj.core import db as dbmod  # utc_now / show_date (D10)
from proj.core.config import ProjConfig
from proj.core.templates import ChangelogSpec, RecordTypeSpec, load_changelog_spec

SYM = {"Draft": "○", "In Progress": "◐", "Failed": "✗", "Done": "✓"}


@dataclass
class View:
    path: Path
    text: str




def status_legend(spec: RecordTypeSpec) -> list[str]:
    """One-line legend for below status tables (table views only), rendered
    *italic* (markdown's 'small print' — like the one-sentence summary line)
    so the emoji-only Status/Change columns stay decodable:
    '_🌱 Proposed · ✅ Accepted ..._' — every allowed status."""
    return ["_" + " · ".join(f"{spec.emojify(s)} {s}" for s in spec.status_allowed) + "_"]


def _item_expansion(r: dict, registry: dict | None, db, depth: int) -> list[str]:
    """The task's OWN item checklist (its `items:` section, plain-text lines
    interleaved) indented under the task line (depth+1) — a full expansion
    (top-level items + sub-items + text), so the list shows progress. [] when the
    task has no items."""
    tspec = registry.get("task") if registry else None
    if tspec is None or tspec.items is None or db is None:
        return []
    from proj.engine import items as items_mod  # lazy: views↔items cycle
    sec = items_mod.render_section(items_mod.load(db, r["id"]), tspec)
    if not sec:
        return []
    pad = "  " * (depth + 1)
    # group headings would break the outer structure: render them as bold lines
    out = []
    for line in sec.splitlines():
        m = re.match(r"^### ([A-Z]) · (.+)$", line)
        if m:
            out.append(f"{pad}**{m.group(1)} · {m.group(2)}**")
        else:
            out.append(f"{pad}{line}" if line.strip() else "")
    return out


def _task_line(r: dict, depth: int, registry: dict | None,
               record_dir: Path | None, task_dir: Path | None,
               db=None) -> str:
    """One task line: checkbox + hyperlink (id-slug filename, path relative to
    `record_dir` — the dir the containing view sits in); `[x]` when the task is
    in a TERMINAL status (Done or Failed — both are finished), else `[ ]`;
    a non-done terminal status (Failed) gets its status emoji before the link.
    The task's item checklist (if any) is expanded under the line by the caller
    (`_item_expansion`). `task_dir` is the tasks' absolute dir (root/spec.dir)."""
    tspec = registry.get("task") if registry else None
    if tspec is not None and record_dir is not None and task_dir is not None:
        from proj.engine.store import slugify  # lazy: views↔store cycle
        stem = f"{r['id']}-{slugify(r['title'])}"
        rel = os.path.relpath(task_dir / f"{stem}.md", record_dir).replace(os.sep, "/")
        label = f"[{stem}]({rel})"
    else:
        label = f"**{r['id']}** {r['title']}"
    terminal = tspec is not None and r["status"] in tspec.status_terminal
    box = "x" if terminal else " "
    emoji = ""
    if terminal and (tspec is None or r["status"] not in tspec.status_done):
        emoji = f"{tspec.emojify(r['status'])} " if tspec is not None else f"{r['status']} "
    pad = "  " * depth
    return f"{pad}- [{box}] {emoji}{label}  "


def task_tree_lines(rows: list[dict], parent_key: str, db,
                    depth: int = 0, on_path: set[str] | None = None,
                    registry: dict | None = None, record_dir: Path | None = None,
                    task_dir: Path | None = None) -> list[str]:
    """Render the tasks parented at parent_key ("" = top-level) as a tree:
    each task as a checkbox + hyperlink, its sub-tasks (kind=parent edge,
    source=parent) indented — at any depth (no 2-level cap; the whole task
    tree is shown expanded). Children keep id order. A task already on the
    ancestor chain is skipped (cycle guard).

    Each task line is a checkbox + hyperlink: `- [x] [T-000001-slug](../tasks/…)`
    — `[x]` when the task is in a done status, `[ ]` otherwise; the link text
    is the task's full filename (id-slug, same convention as the head refs) and
    the path is relative to `record_dir` (the containing record's own dir, or the
    project root for TASKLIST.md); without registry/record_dir it falls back to
    plain text."""
    on_path = set() if on_path is None else on_path
    lines: list[str] = []
    if parent_key:
        kids = {c["target"] for c in db.children_of(parent_key)}
    else:
        # top level: tasks with no parent edge at all
        kids = {t["id"] for t in rows if not db.parent_of(t["id"])}
    for r in rows:
        if r["id"] not in kids:
            continue
        if r["id"] in on_path:  # cycle guard
            continue
        child_path = on_path | {r["id"]}
        # trailing two spaces = markdown hard break: every tree line stays on
        # its own line in any renderer (checkbox + hyperlink + sub-tasks)
        lines.append(_task_line(r, depth, registry, record_dir, task_dir, db))
        lines += _item_expansion(r, registry, db, depth)
        lines += task_tree_lines(rows, r["id"], db, depth + 1, child_path,
                                 registry, record_dir, task_dir)
    return lines


def task_ids_of(db, rows: list[dict], root_id: str) -> set[str]:
    """Every task id parented at root_id (transitively, any depth, cycle-safe)."""
    by_id = {r["id"]: r for r in rows}
    out: set[str] = set()
    seen: set[str] = set()
    stack = [root_id]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for c in db.children_of(cur):
            if by_id.get(c["target"]) is not None and c["target"] not in out:
                out.add(c["target"])
                stack.append(c["target"])
    return out


# a stored Tasks-block line that is (or looks like) a task link:
# `  - [x] [T-000001-slug](path)  trailing user text`
_TASK_LINE_RE = re.compile(
    r"^(\s*)- \[[ xX]\] \[([A-Z]+-[0-9A-Za-z]+[^\]]*)\]\(([^)]+)\)\s*(.*)$")


def render_tasks_block(stored: list[str], rows: list[dict], tree_ids: set[str], db,
                       task_spec: RecordTypeSpec | None, registry: dict | None,
                       record_dir: Path | None, task_dir: Path | None) -> list[str]:
    """The Tasks block content for one record md. Stored user lines (captured on
    sync) come first, verbatim — a line that is a task link is re-emitted with the
    task's CURRENT status so its checkbox tracks the DB, trailing text
    (description / result) kept on the same line. Tasks the record parents but
    that have no stored line are appended (flat, id order)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in stored:
        m = _TASK_LINE_RE.match(line)
        if not m:
            out.append(line)            # hand-written line: kept verbatim
            continue
        pad, stem, path, desc = m.groups()
        r = (db.get(task_spec, re.match(r"[A-Z]+-[0-9A-Za-z]+", stem).group(0))
             if task_spec else None)
        if r is None:                   # looks like a task link but is not one
            out.append(line + "  ")
            continue
        seen.add(r["id"])
        terminal = r["status"] in task_spec.status_terminal
        box = "x" if terminal else " "
        emoji = f"{task_spec.emojify(r['status'])} " if (
            terminal and r["status"] not in task_spec.status_done) else ""
        out.append(f"{pad}- [{box}] {emoji}[{stem}]({path})" + (f" {desc}" if desc else "") + "  ")
    for r in rows:
        if r["id"] in tree_ids and r["id"] not in seen:
            out.append(_task_line(r, 0, registry, record_dir, task_dir, db))
    return out


def render_tasklist_md(cfg: ProjConfig, db, registry: dict[str, RecordTypeSpec]) -> View | None:
    """The global TASKLIST.md — membership is DERIVED, nothing is registered:

    1. `## Unparented`: every task with no parent edge AND not in a terminal
       status, each with its full task tree (the section is omitted when there
       is nothing to show).
    2. then one section per NON-task record that (a) is not in its type's first
       status (usually Draft) and not in its terminal statuses, and (b) has at
       least one non-terminal descendant task — in registry type order, then id.

    Every section lists its record's full task tree (finished tasks included,
    so the list doubles as a progress record). The per-record Tasks block
    (right under the head fence) is independent of this list."""
    task_spec = registry.get("task")
    if task_spec is None:
        return None
    tasks = db.all(task_spec)
    lines = ["# Tasklist", ""]
    body: list[str] = []

    def root_heading(r: dict, spec: RecordTypeSpec) -> str:
        """A `##` section heading for one tree root: emoji + `type ID` + title."""
        return f"## {spec.emojify(r['status'])} {spec.record_type} {r['id']} {r['title']}"

    def tree(root: str | None) -> list[str]:
        return task_tree_lines(tasks, root, db, depth=1,
                               registry=registry, record_dir=cfg.root,
                               task_dir=cfg.root / task_spec.dir)

    # 1) Unparented: tasks with no parent edge and not yet finished
    unparented = [t for t in tasks
                  if not db.parent_of(t["id"])
                  and t["status"] not in task_spec.status_terminal]
    if unparented:
        body += ["## Unparented", ""]
        for t in unparented:
            body += [_task_line(t, 0, registry, cfg.root, cfg.root / task_spec.dir, db)]
            body += _item_expansion(t, registry, db, 0)
            body += task_tree_lines(tasks, t["id"], db, depth=1,
                                    registry=registry, record_dir=cfg.root,
                                    task_dir=cfg.root / task_spec.dir)
        body.append("")

    # 2) non-task records: past the first status, not terminal, with a
    #    non-terminal descendant task
    for record_type, spec in registry.items():
        if record_type == "task" or not spec.status_allowed:
            continue
        for row in db.all(spec):
            if row["status"] in spec.status_terminal:
                continue
            if row["status"] == spec.status_allowed[0]:
                continue  # not started yet (usually Draft): its tasks stay hidden
            if not any(t["id"] in db.descendants_of(row["id"])
                       and t["status"] not in task_spec.status_terminal
                       for t in tasks):
                continue
            body += [root_heading(row, spec), ""]
            body += tree(row["id"])
            body.append("")

    lines += body or ["_no open tasks_"]
    lines += ["", "_Rendered by `proj update` — never hand-edit. "
              "A record shows up while it is past its first status and has an "
              "unfinished descendant task; tasks with no parent sit under "
              "`Unparented`. Edit task files under `docs/tasks/` instead._"]
    return View(cfg.root / "TASKLIST.md", "\n".join(lines) + "\n")


# ---- global CHANGELOG.md -------------------------------------------------------

VERSION_HEAD_RE = re.compile(r"^## \[([^\]]+)\](- (\d{4}-\d{2}-\d{2}))?$")


def render_changelog_md(cfg: ProjConfig, db, cl: ChangelogSpec | None = None) -> View:
    cl = cl or load_changelog_spec(cfg.root)
    unreleased = cl.unreleased
    lines = ["# Changelog", "",
             # hard breaks (two trailing spaces): each intro line on its own line
             "All notable changes to this project will be documented in this file.  ",
             "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),  ",
             "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).",
             "", f"## [{unreleased}]", ""]
    unreleased_entries = [e for e in db.cl_entries(unreleased)]
    lines += _cl_sections(unreleased_entries, cl)
    released = [e for e in db.cl_entries() if e["version"] != unreleased]
    by_version: dict[str, list[dict]] = {}
    for e in released:
        by_version.setdefault(e["version"], []).append(e)
    versions = sorted(by_version, key=lambda v: [int(x) if x.isdigit() else 0
                                                 for x in re.split(r"[.\-]", v)],
                      reverse=True)
    if versions:
        def _block(v: str) -> list[str]:
            newest = dbmod.show_date(max(e["date"] for e in by_version[v]))  # D10
            return [f"## [{v}] - {newest}", "", *_cl_sections(by_version[v], cl)]
        # newest released version stays visible; older ones collapse into a
        # single <details> (summary = the historical version range)
        lines += _block(versions[0])
        if len(versions) > 1:
            older = versions[1:]
            lines += ["", "<details>",
                      f"<summary>点击展开历史版本 ({older[-1]} - {older[0]})</summary>", ""]
            for v in older:
                lines += _block(v)
            lines += ["</details>", ""]
    return View(cfg.root / cl.file, "\n".join(lines).rstrip() + "\n")


def _cl_sections(entries: list[dict], cl: ChangelogSpec) -> list[str]:
    lines: list[str] = []
    for cat in cl.categories:
        items = [e["message"] for e in entries if e["category"] == cat]
        if not items:
            continue
        lines += [f"### {cat}", ""]
        lines += [f"- {t}" for t in items]
        lines.append("")
    return lines


# ---- version helpers -----------------------------------------------------------

def read_versions(cfg: ProjConfig, db, cl: ChangelogSpec | None = None) -> list[tuple[str, str]]:
    """[(version, date)] for released versions, newest first (from entries)."""
    cl = cl or load_changelog_spec(cfg.root)
    entries = [e for e in db.cl_entries() if e["version"] != cl.unreleased]
    out: dict[str, str] = {}
    for e in entries:
        out[e["version"]] = dbmod.show_date(
            max(out.get(e["version"], ""), e["date"]))  # D10: date part
    # numeric sort (semver), not string sort — 'v1.10.0' > 'v1.9.0'
    return sorted(out.items(),
                  key=lambda kv: [int(x) for x in re.findall(r"\d+", kv[0])][:3],
                  reverse=True)


def bump(version: str, level: str) -> str:
    parts = [int(x) for x in re.findall(r"\d+", version)][:3]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts
    if level == "major":
        major, minor, patch = major + 1, 0, 0
    elif level == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def release(cfg: ProjConfig, db, version: str | None = None,
            bump_level: str | None = None) -> str:
    """Move unreleased entries into a version. Returns the version used."""
    cl = load_changelog_spec(cfg.root)
    entries = db.cl_entries(cl.unreleased)
    current: str | None = None
    rows = db.conn.execute(
        "SELECT DISTINCT version FROM changelog_entries WHERE version != ?",
        (cl.unreleased,)).fetchall()
    if rows:
        # highest version wins (semver), not most recently dated — backfilling
        # an older release must not skew the bump base
        current = max((r[0] for r in rows),
                      key=lambda v: [int(x) for x in re.findall(r"\d+", v)][:3])
    if version:
        used = version
    elif bump_level:
        if current is None:
            current = "0.0.0"
        used = f"v{bump(current.lstrip('v'), bump_level)}"
    else:
        raise ValueError("need --version or --bump")
    db.conn.execute(
        "UPDATE changelog_entries SET version = ? WHERE version = ?",
        (used, cl.unreleased))
    if not entries:
        db.add_cl_entry(used, cl.default_category, f"release {used}",
                        dbmod.utc_now())
    db.conn.commit()
    return used
