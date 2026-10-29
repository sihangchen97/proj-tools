"""CHANGELOG.md maintenance (Keep a Changelog style).

Structure:

    # Changelog

    ## [Unreleased]
    ...

    ## [0.2.0] - 2026-10-28
    ### Added
    - item
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SECTION_NAMES = ("Added", "Changed", "Fixed", "Deprecated", "Removed", "Security")
VER_RE = re.compile(r"^## \[([^\]]+)\]\s*(?:-\s*(\d{4}-\d{2}-\d{2}))?\s*$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass
class Version:
    name: str  # "Unreleased" or "0.2.0"
    date: str | None
    start: int  # line index of the "## [...]" header
    end: int  # exclusive line index of the next version header (or EOF)


def _parse_versions(lines: list[str]) -> list[Version]:
    out: list[Version] = []
    from itertools import pairwise
    for i, line in enumerate(lines):
        m = VER_RE.match(line.strip())
        if m:
            out.append(Version(name=m.group(1), date=m.group(2), start=i, end=len(lines)))
    for prev, nxt in pairwise(out):
        prev.end = nxt.start
    return out


def read(cfg) -> tuple[list[str], list[Version]]:
    path: Path = cfg.changelog
    if not path.is_file():
        return ["# Changelog", ""], []
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines, _parse_versions(lines)


def write(cfg, lines: list[str]) -> None:
    cfg.changelog.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _ensure_header(lines: list[str]) -> list[str]:
    if not lines or lines[0].strip() != "# Changelog":
        return ["# Changelog", "", *lines]
    return lines


def _current_version(lines: list[str], pyproject_version: str | None) -> str:
    """Highest released version: first non-Unreleased entry, else pyproject."""
    for v in _parse_versions(lines):
        if v.name.lower() != "unreleased" and SEMVER.match(v.name):
            return v.name
    return pyproject_version or "0.0.0"


def bump(current: str, part: str) -> str:
    m = SEMVER.match(current)
    if not m:
        raise ValueError(f"'{current}' is not a semver version")
    major, minor, patch = (int(g) for g in m.groups())
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:
        raise ValueError(f"unknown bump: {part}")
    return f"{major}.{minor}.{patch}"


def version(cfg, part: str | None = None, explicit: str | None = None) -> str:
    """Create a new released section (and a fresh [Unreleased]) on top.

    Returns the new version string.
    """
    lines, versions = read(cfg)
    lines = _ensure_header(lines)
    if explicit:
        new = explicit
    elif part:
        new = bump(_current_version(lines, _pyproject_version(cfg)), part)
    else:
        raise ValueError("provide a version or --bump")
    if any(v.name == new for v in _parse_versions(lines)):
        raise ValueError(f"version {new} already exists")
    # released versions go after [Unreleased] (if any), newest first
    versions = _parse_versions(lines)
    unreleased = next((v for v in versions if v.name.lower() == "unreleased"), None)
    released = [v for v in versions if v.name.lower() != "unreleased"]
    if released:
        insert_at = released[0].start
    elif unreleased:
        insert_at = unreleased.end
        while insert_at > 0 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
    else:
        insert_at = len(lines)
        while lines and lines[-1].strip() == "":
            lines.pop()
    block = [f"## [{new}] - {date.today().isoformat()}", ""]
    if unreleased is None:
        block += ["## [Unreleased]", ""]
    if insert_at and lines[insert_at - 1].strip() != "":
        block.insert(0, "")
    lines[insert_at:insert_at] = block
    write(cfg, lines)
    return new


def _pyproject_version(cfg) -> str | None:
    import tomllib
    p = cfg.root / "pyproject.toml"
    if p.is_file():
        with p.open("rb") as f:
            return tomllib.load(f).get("project", {}).get("version")
    return None


def add_entry(cfg, section_name: str, text: str, version_name: str = "Unreleased") -> None:
    name = next((s for s in SECTION_NAMES if s.lower() == section_name.lower()), None)
    if name is None:
        raise ValueError(f"section must be one of {', '.join(SECTION_NAMES)}")
    lines, versions = read(cfg)
    lines = _ensure_header(lines)
    versions = _parse_versions(lines)
    ver = next((v for v in versions if v.name == version_name), None)
    if ver is None:
        # create the version section at the top (before first existing version)
        insert_at = versions[0].start if versions else len(lines)
        lines[insert_at:insert_at] = [f"## [{version_name}]", ""]
        versions = _parse_versions(lines)
        ver = next(v for v in versions if v.name == version_name)
    section_header = f"### {name}"
    # find (or create) the subsection within this version
    sub_idx = None
    for i in range(ver.start + 1, ver.end):
        if lines[i].strip() == section_header:
            sub_idx = i
            break
    if sub_idx is None:
        lines[ver.end:ver.end] = [section_header, f"- {text}", ""]
    else:
        lines[sub_idx + 1:sub_idx + 1] = [f"- {text}"]
    write(cfg, lines)


def release_notes(cfg, version_name: str) -> str:
    lines, versions = read(cfg)
    ver = next((v for v in versions if v.name == version_name), None)
    if ver is None:
        raise ValueError(f"version {version_name} not found")
    body = "\n".join(lines[ver.start + 1: ver.end]).strip()
    return f"# {version_name}\n\n{body}\n"
