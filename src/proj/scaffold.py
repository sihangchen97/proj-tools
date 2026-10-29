"""`proj init` — scaffold project governance files. Idempotent, never overwrites."""

from __future__ import annotations

from pathlib import Path

from proj.config import ProjConfig
from proj.templates import bundled_dir, load_registry, render

CORE_DIRS = ["docs/decisions", "docs/plans"]
TIER_DIRS = {
    "cv": ["docs/experiments"],
    "infra": ["docs/specs", "docs/dev"],
    "quality": ["docs/tech-debt", "docs/incidents"],
}

CHANGELOG_INIT = """\
# Changelog

## [Unreleased]
"""

TASKLIST_INIT = """\
# Tasklist

Active work only. Finish with `proj task done <title>` (archives to docs/plans/),
or park unfinished work with `proj plan shelve <title>`.
"""

def _seed_decision_0001(cfg: ProjConfig) -> bool:
    """Create the first decision (0001) via the v2 engine. True if created."""
    import uuid
    spec = load_registry(cfg.root).get("decision")
    if spec is None:
        return False
    d = cfg.root / spec.dir
    if any(d.glob("[0-9][0-9][0-9][0-9]-*.md")) if d.is_dir() else False:
        return False
    body = render(spec, uuid=str(uuid.uuid4()), id_="0001",
                  title="Record architecture decisions", date=_today())
    body = body.replace("[Problem statement & constraints]",
                        "We want a lightweight, reviewable record of non-trivial "
                        "architectural decisions and the trade-offs behind them.")
    body = body.replace("[Chosen solution & why over alternatives]",
                        "We use Architecture Decision Records managed by the `proj` "
                        "tool, stored as frontmatter-marked Markdown files under "
                        "`docs/decisions/`.")
    body = body.replace("[Pros, cons, tech debt, and follow-ups]",
                        "Every significant decision gets a record before production "
                        "code. Regenerate the TOC with `proj decision toc`.")
    body = body.replace("status: Proposed", "status: Accepted", 1)
    d.mkdir(parents=True, exist_ok=True)
    (d / "0001-record-architecture-decisions.md").write_text(body, encoding="utf-8")
    return True

DATASET_INIT = """\
# DATASET

## Data & Coordinate Systems

| Dataset | Location | Resolution / shape | Channel order | Coordinate system | Notes |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

Record input dimensions, channel order (RGB vs BGR), coordinate systems,
and mount/storage paths explicitly for every dataset in use.
"""

BENCHMARK_INIT = """\
# BENCHMARK

## Hardware & Latency Footprint

| Test | Hardware (e.g. RTX 4090) | VRAM used | FPS / latency | Date | Replication script |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

Always record baseline hardware and how to reproduce the measurement.
"""

ENV_EXAMPLE_INIT = """\
# Example environment variables (copy to .env, never commit .env)
# NAMES ONLY — no real values in this file.
# API_KEY=
"""

CONTRIBUTING_INIT = """\
# Contributing

## Workflow rules

1. **ADR first**: before significant architectural, dependency or API changes,
   create an ADR (`proj decision new "Title"`) and get it Accepted. Do not write
   production code for big changes until the ADR is discussed/accepted.
2. **Task tracking**: multi-step work lives in `TASKLIST.md` with atomic,
   verifiable sub-steps. Mark steps `[x]` only after local verification
   (tests/lint). Archive finished work: `proj task done <title>` → `docs/plans/`.
   Park work you're pausing: `proj plan shelve <title>`.
3. **Changelog**: on milestones, features, refactors or bugfixes add an entry:
   `proj changelog add "Added <text>"`.
4. **Tech debt**: temporary hacks get their own file (`proj debt new "Title"`)
   with Location, Reason, and Trigger condition for removal.
5. **Secrets**: never hardcode secrets, private IPs or tokens. Read from the
   environment; commit only `.env.example`.
6. **Commits**: one commit per feature or bugfix. No unrequested reformatting
   of unrelated files.
7. **Check**: run `proj check` (also runs in pre-commit) before pushing.
"""

ISSUE_BUG = """\
---
name: Bug report
about: Something is broken
---

## What happened?

## Expected behavior

## Steps to reproduce

1.

## Environment (OS, versions, commit hash)

## Related ADR / tasklist entry (if any)
"""

ISSUE_FEATURE = """\
---
name: Feature request
about: A new capability or change
---

## Problem to solve

## Proposed solution

## Alternatives considered

## ADR required? (yes for architectural changes — link it here)
"""

PR_TEMPLATE = """\
## Summary

## Tasklist / ADR references

- TASKLIST entry:
- ADR:

## Verification

- [ ] `proj check` passes
- [ ] tests pass locally
- [ ] CHANGELOG.md updated (`proj changelog add ...`) where applicable
"""


def _write_if_new(root: Path, rel: str, content: str) -> bool:
    p = root / rel
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return True


def init(cfg: ProjConfig, tiers: list[str]) -> list[str]:
    """Create missing governance files. Returns list of created paths."""
    created: list[str] = []

    def add(rel: str, content: str) -> None:
        if _write_if_new(cfg.root, rel, content):
            created.append(rel)

    for d in CORE_DIRS + [d for t in tiers for d in TIER_DIRS.get(t, [])]:
        p = cfg.root / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            created.append(d + "/")

    # .proj/ — tool-owned files
    p = cfg.proj_dir
    if not p.is_dir():
        p.mkdir(parents=True, exist_ok=True)
        created.append(".proj/")

    # templates/default/ — copy bundled templates the user doesn't have yet
    for f in (bundled_dir() / "default").glob("*.md"):
        rel = f"templates/default/{f.name}"
        add(rel, f.read_text(encoding="utf-8"))

    add("CHANGELOG.md", CHANGELOG_INIT)
    add("TASKLIST.md", TASKLIST_INIT)
    # decision 0001 only if none exist yet
    if _seed_decision_0001(cfg):
        created.append("docs/decisions/0001-record-architecture-decisions.md")
    if "cv" in tiers:
        add("DATASET.md", DATASET_INIT)
        add("BENCHMARK.md", BENCHMARK_INIT)
    if "quality" in tiers:
        add(".env.example", ENV_EXAMPLE_INIT)
    add("CONTRIBUTING.md", CONTRIBUTING_INIT)
    add(".github/ISSUE_TEMPLATE/bug.md", ISSUE_BUG)
    add(".github/ISSUE_TEMPLATE/feature.md", ISSUE_FEATURE)
    add(".github/PULL_REQUEST_TEMPLATE.md", PR_TEMPLATE)
    return created


def _today() -> str:
    from datetime import date
    return date.today().isoformat()
