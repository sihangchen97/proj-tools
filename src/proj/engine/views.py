"""Rendered views (generated — never hand-edited): WIP.md, CHANGELOG.md,
and the per-record Tasks block (a record's task tree from the DB)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from proj.core import db as dbmod  # utc_now / show_date (D10)
from proj.core.config import ProjConfig
from proj.core.templates import ChangelogSpec, RecordTypeSpec, load_changelog_spec


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


def _task_line(r: dict, depth: int, registry: dict | None,
               record_dir: Path | None, task_dir: Path | None,
               db=None, summary: str = "") -> str:
    """One task line: checkbox + hyperlink (id-slug filename, path relative to
    `record_dir` — the dir the containing view sits in); `[x]` when the task is
    in a TERMINAL status (Done or Failed — both are finished), else `[ ]`;
    a non-done terminal status (Failed) gets its status emoji before the link;
    the task's one-line summary (if any) trails the link in *italic* (Q5).
    `task_dir` is the tasks' abs dir."""
    tspec = registry.get("task") if registry else None
    if tspec is not None and record_dir is not None and task_dir is not None:
        from proj.engine.store import slugify  # lazy: views↔store cycle
        stem = f"{r['id']}-{slugify(r['title'])}"
        rel = os.path.relpath(task_dir / f"{stem}.md", record_dir).replace(os.sep, "/")
        label = f"[{stem}]({rel})"
    else:
        label = f"**{r['id']}** {r['title']}"
    summary = (summary or "").strip()
    if summary:
        label = f"{label} — _{summary}_"
    terminal = tspec is not None and r["status"] in tspec.status_terminal
    box = "x" if terminal else " "
    emoji = ""
    if terminal and (tspec is None or r["status"] not in tspec.status_done):
        emoji = f"{tspec.emojify(r['status'])} " if tspec is not None else f"{r['status']} "
    pad = "  " * depth
    return f"{pad}- [{box}] {emoji}{label}  "


def render_tasks_block(task_rows: list[dict], db, task_spec: RecordTypeSpec,
                       registry: dict | None, record_dir: Path | None,
                       task_dir: Path | None, root_id: str) -> list[str]:
    """The Tasks block content for one record md (A2/Q5: generated ENTIRELY from
    the DB — the record's task tree via kind=parent edges, indented by tree
    depth, each line a checkbox + hyperlink + the task's one-line summary).
    Nothing task-related is stored in the md, so a re-render can never fight a
    hand copy and a Failed task line can no longer be misread as a hand line
    (the old stored-line path, P0-4, is gone)."""
    by_id = {r["id"]: r for r in task_rows}
    out: list[str] = []
    seen: set[str] = {root_id}

    def emit(parent_id: str, depth: int) -> None:
        for c in db.children_of(parent_id):
            r = by_id.get(c["target"])
            if r is None or r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(_task_line(r, depth, registry, record_dir, task_dir, db,
                                  summary=r.get("summary") or ""))
            emit(r["id"], depth + 1)

    emit(root_id, 0)
    return out


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
