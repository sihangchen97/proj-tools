"""Rendered views (generated — never hand-edited): TASKLIST.md, CHANGELOG.md,
and per-record view sections (e.g. plan's Task List)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from proj.core.config import ProjConfig
from proj.core.templates import ChangelogSpec, RecordTypeSpec, load_changelog_spec
from proj.engine.store import checkboxes

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


def _sub_task_lines(steps: str) -> list[str]:
    """Two-level checkbox list: top steps with symbols, sub-tasks indented.
    A top step is ✓ when checked, ◐ when unchecked but it has its own
    sub-level steps (work in flight), ○ when it has none (not started)."""
    out: list[str] = []
    items = checkboxes(steps)
    if not items:
        return out
    top_indent = min(i for i, _, _ in items)
    for idx, (indent, checked, text) in enumerate(items):
        line = re.sub(r"^\s*[-*] \[[ xX]\]\s*", "", text)
        if indent == top_indent:
            own = []
            for it in items[idx + 1:]:
                if it[0] == top_indent:
                    break  # next top-level step
                own.append(it)
            mark = "✓" if checked else ("◐" if own else "○")
            out.append(f"  - {mark} {line}")
        else:
            out.append(f"    - {'✓' if checked else '○'} {line}")
    return out


def task_tree_lines(rows: list[dict], parent_key: str, db,
                    depth: int = 0, on_path: set[str] | None = None) -> list[str]:
    """Render the tasks parented at parent_key ("" = top-level) as a tree:
    each task's local steps below it, then its sub-tasks (kind=parent edge,
    source=parent) indented. Children keep id order (= creation order). A task
    already on the ancestor chain is skipped (cycle guard)."""
    on_path = set() if on_path is None else on_path
    lines: list[str] = []
    pad = "  " * depth
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
        sections = json.loads(r["sections"] or "{}")
        lines.append(f"{pad}- {SYM.get(r['status'], '?')} **{r['id']}** {r['title']}")
        lines += [f"{pad}  " + s for s in _sub_task_lines(sections.get("steps", ""))]
        open_items = sections.get("open", "").strip()
        for item in open_items.splitlines():
            if item.strip():
                lines.append(f"{pad}  - open: {item.strip()}")
        lines += task_tree_lines(rows, r["id"], db, depth + 1, child_path)
    return lines


def render_tasklist_md(cfg: ProjConfig, db, registry: dict[str, RecordTypeSpec]) -> View | None:
    task_spec = registry.get("task")
    if task_spec is None:
        return None
    tasks = db.all(task_spec)
    lines = ["# Tasklist", ""]
    body: list[str] = []
    # top level: every contain_task record (plan, experiment, …) renders as a
    # tree root — its tasks hang indented below (kind=parent edges);
    # then parentless tasks.
    for spec in registry.values():
        if spec is task_spec or not spec.contain_task:
            continue
        for r in db.all(spec):
            head = f"- {spec.emojify(r['status'])} **{spec.record_type} {r['id']}** {r['title']}"
            body.append(head)
            body += task_tree_lines(tasks, r["id"], db, depth=1)
    parented = {t["id"] for t in tasks if db.parent_of(t["id"])}
    body += task_tree_lines(
        [t for t in tasks if t["id"] not in parented], "", db)
    lines += body or ["_no tasks_"]
    lines += ["", "_Rendered by `proj update` — never hand-edit. "
              "Edit task files under `docs/tasks/` instead._"]
    return View(cfg.root / "TASKLIST.md", "\n".join(lines) + "\n")


# ---- global CHANGELOG.md -------------------------------------------------------

VERSION_HEAD_RE = re.compile(r"^## \[([^\]]+)\](- (\d{4}-\d{2}-\d{2}))?$")


def render_changelog_md(cfg: ProjConfig, db, cl: ChangelogSpec | None = None) -> View:
    cl = cl or load_changelog_spec(cfg.root)
    unreleased = cl.unreleased
    lines = ["# Changelog", "",
             "All notable changes to this project will be documented in this file.",
             "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),",
             "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).",
             "", f"## [{unreleased}]", ""]
    unreleased_entries = [e for e in db.cl_entries(unreleased)]
    lines += _cl_sections(unreleased_entries, cl)
    released = [e for e in db.cl_entries() if e["version"] != unreleased]
    by_version: dict[str, list[dict]] = {}
    for e in released:
        by_version.setdefault(e["version"], []).append(e)
    for version in sorted(by_version, key=lambda v: [int(x) if x.isdigit() else 0
                                                    for x in re.split(r"[.\-]", v)]
                          , reverse=True):
        entries = by_version[version]
        newest = max(e["date"] for e in entries)
        lines += [f"## [{version}] - {newest}", ""]
        lines += _cl_sections(entries, cl)
    return View(cfg.root / cl.file, "\n".join(lines).rstrip() + "\n")


def _cl_sections(entries: list[dict], cl: ChangelogSpec) -> list[str]:
    lines: list[str] = []
    for cat in cl.categories:
        items = [e["text"] for e in entries if e["category"] == cat]
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
        out[e["version"]] = max(out.get(e["version"], ""), e["date"])
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
                        date.today().isoformat())
    db.conn.commit()
    return used
