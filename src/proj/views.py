"""Rendered views (generated — never hand-edited): TASKLIST.md, CHANGELOG.md,
and per-record view sections (e.g. plan's Task List)."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from proj import db as dbmod
from proj.config import ProjConfig
from proj.records import checkboxes
from proj.templates import KindSpec

SYM = {"Not started": "○", "In progress": "◐", "Done": "✓"}
CL_ORDER = list(dbmod.CL_CATEGORIES)


@dataclass
class View:
    path: Path
    text: str


def _norm_id(v: str) -> str:
    return str(int(v)) if v.isdigit() else v


def _visible_tasks(spec: KindSpec, row: dict, db: sqlite3.Connection | None,
                   plan_rows: dict[str, dict] | None) -> bool:
    """Task visibility: main tasklist only shows tasks with no plan, or whose
    plan is Active. Otherwise they live in their plan's Task List section."""
    plan_id = _norm_id((row.get("plan") or "").strip())
    if not plan_id:
        return True
    p = plan_rows.get(plan_id) if plan_rows else None
    return bool(p and p["status"] == "Active")


def _task_line(id_: str, title: str, status: str) -> str:
    return f"{SYM.get(status, '?')} {id_} — {title}"


def _sub_task_lines(steps: str) -> list[str]:
    """Two-level checkbox list: top steps with symbols, sub-tasks indented."""
    out: list[str] = []
    items = checkboxes(steps)
    if not items:
        return out
    top_indent = min(i for i, _, _ in items)
    for indent, checked, text in items:
        mark = "✓" if checked else ("◐" if any(c for i, c, _ in items) else "○")
        line = re.sub(r"^\s*[-*] \[[ xX]\]\s*", "", text)
        if indent == top_indent:
            out.append(f"  - {mark} {line}")
        else:
            out.append(f"    - {'✓' if checked else '○'} {line}")
    return out


def render_view(name: str, spec: KindSpec, row: dict, db,
                task_spec: KindSpec | None = None) -> list[str]:
    if name == "tasks" and spec.kind == "plan":
        return _render_plan_tasklist(row["id"], db, task_spec)
    raise ValueError(f"unknown view renderer {name!r}")


def _render_plan_tasklist(plan_id: str, db, task_spec: KindSpec | None) -> list[str]:
    rows = db.all(task_spec) if task_spec else []
    target = _norm_id(plan_id)
    lines: list[str] = []
    for r in rows:
        if _norm_id((r.get("plan") or "").strip()) != target:
            continue
        sections = json.loads(r["sections"] or "{}")
        lines.append(f"- {SYM.get(r['status'], '?')} **{r['id']}** {r['title']}")
        lines += _sub_task_lines(sections.get("steps", ""))
        open_items = sections.get("open", "").strip()
        if open_items:
            for item in open_items.splitlines():
                if item.strip():
                    lines.append(f"  - open: {item.strip()}")
    return lines or ["(no tasks attached)"]


def render_tasklist_md(cfg: ProjConfig, db, registry: dict[str, KindSpec]) -> View | None:
    if "task" not in registry:
        return None
    task_spec, plan_spec = registry["task"], registry.get("plan")
    plan_rows: dict[str, dict] = {_norm_id(r["id"]): r
                                  for r in db.all(plan_spec)} \
        if plan_spec else {}
    lines = ["# Tasklist", ""]
    main, parked = [], 0
    for r in db.all(task_spec):
        if _visible_tasks(task_spec, r, db, plan_rows):
            main.append(r)
        else:
            parked += 1
    if main:
        for r in main:
            sections = json.loads(r["sections"] or "{}")
            lines.append(f"- {SYM.get(r['status'], '?')} **{r['id']}** {r['title']}")
            lines += _sub_task_lines(sections.get("steps", ""))
            if (r.get("plan") or "").strip():
                lines.append(f"  - (plan {r['plan'].strip()})")
    else:
        lines.append("_no active tasks_")
    if parked:
        lines.append("")
        lines.append(f"_{parked} task(s) parked in their plans (see plan Task List sections)_")
    lines.append("")
    lines.append("_Rendered by `proj update` — never hand-edit. "
                 "Edit task files under `tasks/` instead._")
    return View(cfg.root / "TASKLIST.md", "\n".join(lines) + "\n")


# ---- global CHANGELOG.md -------------------------------------------------------

VERSION_HEAD_RE = re.compile(r"^## \[([^\]]+)\](- (\d{4}-\d{2}-\d{2}))?$")


def render_changelog_md(cfg: ProjConfig, db) -> View:
    lines = ["# Changelog", "",
             "All notable changes to this project will be documented in this file.",
             "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),",
             "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).",
             "", "## [Unreleased]", ""]
    unreleased = [e for e in db.cl_entries("Unreleased")]
    lines += _cl_sections(unreleased)
    released = [e for e in db.cl_entries() if e["version"] != "Unreleased"]
    by_version: dict[str, list[dict]] = {}
    for e in released:
        by_version.setdefault(e["version"], []).append(e)
    for version in sorted(by_version, key=lambda v: [int(x) if x.isdigit() else 0
                                                    for x in re.split(r"[.\-]", v)]
                          , reverse=True):
        entries = by_version[version]
        newest = max(e["date"] for e in entries)
        lines += [f"## [{version}] - {newest}", ""]
        lines += _cl_sections(entries)
    return View(cfg.root / "CHANGELOG.md", "\n".join(lines).rstrip() + "\n")


def _cl_sections(entries: list[dict]) -> list[str]:
    lines: list[str] = []
    for cat in CL_ORDER:
        items = [e["text"] for e in entries if e["category"] == cat]
        if not items:
            continue
        lines += [f"### {cat}", ""]
        lines += [f"- {t}" for t in items]
        lines.append("")
    return lines


# ---- version helpers -----------------------------------------------------------

def read_versions(cfg: ProjConfig, db) -> list[tuple[str, str]]:
    """[(version, date)] for released versions, newest first (from entries)."""
    entries = [e for e in db.cl_entries() if e["version"] != "Unreleased"]
    out: dict[str, str] = {}
    for e in entries:
        out[e["version"]] = max(out.get(e["version"], ""), e["date"])
    return sorted(out.items(), reverse=True)


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
    """Move Unreleased entries into a version. Returns the version used."""
    entries = db.cl_entries("Unreleased")
    current: str | None = None
    rows = db.conn.execute(
        "SELECT version, MAX(date) FROM changelog_entries "
        "WHERE version != 'Unreleased' GROUP BY version").fetchall()
    if rows:
        current = max(rows, key=lambda r: r[1])[0]
    if version:
        used = version
    elif bump_level:
        if current is None:
            current = "0.0.0"
        used = f"v{bump(current.lstrip('v'), bump_level)}" if not current.startswith("v") \
            else f"v{bump(current[1:], bump_level)}"
        used = used if used.startswith("v") else "v" + used
    else:
        raise ValueError("need --version or --bump")
    db.conn.execute(
        "UPDATE changelog_entries SET version = ? WHERE version = 'Unreleased'",
        (used,))
    if not entries:
        db.add_cl_entry(used, "Changed", f"release {used}", date.today().isoformat())
    db.conn.commit()
    return used
