"""`proj check` — invariant validation, CI-able, exit 1 on failure."""

from __future__ import annotations

from proj import decision as dec
from proj import index as pindex
from proj.changelog import read as read_changelog
from proj.config import ProjConfig
from proj.tasklist import read_tasks


def check(cfg: ProjConfig) -> list[str]:
    """Return list of violation messages (empty = healthy)."""
    problems: list[str] = []

    # Decisions (v2 frontmatter records): validate each against the contract.
    index = pindex.load(cfg)
    for rec in dec._records(cfg):
        problems.extend(pindex.validate_record(cfg, rec, index))

    # TASKLIST: fully-completed tasks must be archived (not still active).
    _header, blocks = read_tasks(cfg)
    for _title, _lines, task in blocks:
        if task.complete:
            problems.append(f"TASKLIST.md: task '{task.title}' is fully done — "
                            f"run `proj task done {task.title}`")

    # CHANGELOG: entries under the first released version must not precede
    # [Unreleased] (version order), and Unreleased, if present, must be first.
    _lines, versions = read_changelog(cfg)
    released = [v for v in versions if v.name.lower() != "unreleased"]
    if len(released) > 1:
        names = [v.name for v in released]
        if names != sorted(names, key=lambda s: [int(x) for x in s.split(".") if x.isdigit()],
                           reverse=True):
            problems.append(f"CHANGELOG.md: versions out of order: {', '.join(names)}")
    if versions:
        first = versions[0]
        if first.name.lower() != "unreleased" and any(
                v.name.lower() == "unreleased" for v in versions[1:]):
            problems.append("CHANGELOG.md: [Unreleased] must be the first section")

    return problems
