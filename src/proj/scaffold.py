"""`proj init` — scaffold, lock, and adoption (v3). Idempotent."""
from __future__ import annotations

import json

from proj import db as dbmod
from proj import records as rec
from proj.config import ProjConfig
from proj.templates import bundled_dir, load_registry

PROFILE_DIRS = {
    "core": [],
    "cv": ["docs/experiments"],
    "infra": ["docs/specs", "docs/dev"],
    "quality": ["docs/tech-debt", "docs/incidents"],
}

ENV_EXAMPLE_INIT = """\
# Example environment variables (copy to .env, never commit .env)
# NAMES ONLY — no real values in this file.
# API_KEY=
"""

CONTRIBUTING_INIT = """\
# Contributing

## Workflow rules

1. **Decision first**: before significant architectural, dependency or API
   changes, create a decision (`proj new decision "Title"`) and get it
   Accepted. Do not write production code for big changes until it is accepted.
2. **Task tracking**: multi-step work lives as task records
   (`proj new task "Title"`). Mark steps `[x]` only after local verification
   (tests/lint). Finish with `proj task done <ID>`.
3. **Plans**: attach tasks to a plan with `--plan N`; promote/shelve them by
   moving the plan (`proj plan activate|shelve N`).
4. **Changelog**: on milestones add an entry: `proj changelog add "Added: …"`.
5. **Tech debt**: temporary hacks get their own record
   (`proj new debt "Title"`) with Location, Reason, and Trigger for removal.
6. **Secrets**: never hardcode secrets, private IPs or tokens. Read from the
   environment; commit only `.env.example`.
7. **Commits**: one commit per feature or bugfix. No unrequested reformatting.
8. **Check**: run `proj check` (also runs in pre-commit) before pushing.
9. **MD files**: edit `##` section content only, then `proj sync`.
   Never edit the marker line, `Status:`/field lines or the changelog table.
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

## Related decision / task (if any)
"""

ISSUE_FEATURE = """\
---
name: Feature request
about: A new capability or change
---

## Problem to solve

## Proposed solution

## Alternatives considered

## Decision required? (yes for architectural changes — link it here)
"""

PR_TEMPLATE = """\
## Summary

## Task / decision references

- task:
- decision:

## Verification

- [ ] `proj check` passes
- [ ] tests pass locally
- [ ] CHANGELOG.md updated (`proj changelog add ...`) where applicable
"""

SEED_DECISION = {
    "title": "Record architecture decisions",
    "status": "Accepted",
    "sections": {
        "context": ("We want a lightweight, reviewable record of non-trivial "
                    "architectural decisions and the trade-offs behind them."),
        "decision": ("We use decision records managed by the `proj` tool, stored "
                     "as Markdown files under `docs/decisions/` with SQLite as "
                     "the source of truth (`.proj/sqlite`)."),
        "consequences": ("Every significant decision gets a record before "
                         "production code. Views regenerate via `proj update`."),
    },
}


def init(cfg: ProjConfig, profiles: list[str]) -> list[str]:
    """Create missing governance files, build tables from lock or templates,
    seed decision 0001, and adopt any pre-existing managed files."""
    created: list[str] = []

    def add(rel: str, content: str) -> None:
        p = cfg.root / rel
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        created.append(rel)

    # dirs
    dirs = [s.dir for s in load_registry(cfg.root).values()]
    for prof in profiles:
        dirs.extend(PROFILE_DIRS.get(prof, []))
    for d in dirs:
        p = cfg.root / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            created.append(d + "/")

    # .proj/ — tool-owned
    cfg.proj_dir.mkdir(parents=True, exist_ok=True)
    gi = cfg.proj_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("sqlite\n", encoding="utf-8")  # DB not committed by default
        created.append(".proj/.gitignore")

    # templates/ — copy bundled templates the user doesn't have yet
    tdir = cfg.root / "templates" / "default"
    if not tdir.is_dir():
        tdir.mkdir(parents=True, exist_ok=True)
        created.append("templates/default/")
    for f in (bundled_dir() / "default").glob("*.yaml"):
        dst = tdir / f.name
        if not dst.exists():
            dst.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(f"templates/default/{f.name}")

    add("CHANGELOG.md", "")      # rendered from DB on first update
    add("TASKLIST.md", "")       # rendered from DB on first update
    if "quality" in profiles:
        add(".env.example", ENV_EXAMPLE_INIT)
    add("CONTRIBUTING.md", CONTRIBUTING_INIT)
    add(".github/ISSUE_TEMPLATE/bug.md", ISSUE_BUG)
    add(".github/ISSUE_TEMPLATE/feature.md", ISSUE_FEATURE)
    add(".github/PULL_REQUEST_TEMPLATE.md", PR_TEMPLATE)

    # DB: lock wins, else templates
    lock = dbmod.read_lock(cfg.root)
    db = rec.open_db(cfg)
    if lock:
        specs = dbmod.specs_from_lock(lock)
        profiles_used = list(lock.get("profiles", []))
    else:
        specs = load_registry(cfg.root)
        profiles_used = profiles
        dbmod.write_lock(cfg.root, specs, profiles_used)
        created.append(dbmod.LOCK_FILE)
    for spec in specs.values():
        db.ensure_kind(spec)
    db.close()

    # seed decision 0001 only if none exist yet
    if "decision" in specs:
        spec = specs["decision"]
        if not rec.open_db(cfg).all(spec) and not any(
                (cfg.root / spec.dir).glob("*.md")):
            row, p = rec.new_record(cfg, "decision", SEED_DECISION["title"], spec=spec)
            rec.status_set(cfg, "decision", row["id"], SEED_DECISION["status"])
            d = rec.open_db(cfg)
            d.set(spec, row["uuid"], sections=json.dumps(SEED_DECISION["sections"]))
            fresh = d.get(spec, row["uuid"], by="uuid")
            p.write_text(rec.render_record(spec, fresh, d), encoding="utf-8")
            d.close()
            created.append(str(p.relative_to(cfg.root)))

    # adopt any pre-existing managed files, then render views
    problems, notes = rec.adopt(cfg)
    for n in notes:
        created.append(f"adopted: {n}")
    if problems:
        raise ValueError("adoption failed:\n" + "\n".join(problems))
    return created
