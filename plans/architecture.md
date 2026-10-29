# Project Governance Tool — Architecture (v1)

Name: `proj` (CLI: `proj`, package: `proj`).

## 1. Purpose

One CLI implementing the governance system in `rule_draft.md`, replacing adr-tools
(shell scripts) with a single Python tool that manages **all** project documentation
artifacts from `pyproject.toml`-adjacent config:

| Artifact | Tier | Tool ops |
|---|---|---|
| `docs/adr/NNNN-short-title.md` | mandatory | init, new, list, status, supersede, link, toc, graph |
| `TASKLIST.md` + `docs/plans/` | mandatory | add, done, archive, list |
| `CHANGELOG.md` | mandatory | version (bump), add-entry, release-notes, list |
| `docs/tech-debt/NNNN-*.md` | conditional (C) | new, list, repay, close |
| `docs/incidents/YYYY-MM-DD-*.md` | conditional (C) | new, list |
| `docs/experiments/NNNN-*.md` | conditional (A) | new, list, close |
| `DATASET.md`, `BENCHMARK.md`, `.env.example` | conditional | init-scaffold (template only, no lifecycle) |
| `docs/specs/`, `docs/dev/` | conditional (B) | init-scaffold (dirs only) |
| `.github/ISSUE_TEMPLATE/*`, `PULL_REQUEST_TEMPLATE.md` | governance | init-scaffold (issue types + PR checklist) |
| `CONTRIBUTING.md` | governance | init-scaffold (checklist from rule_draft) |
| `.proj/githooks/pre-commit` | governance | `proj hooks install` → writes `.proj/githooks/` + sets `core.hooksPath` |

v1 scope = full lifecycle for ADR/TASKLIST/CHANGELOG/tech-debt/incidents/experiments +
template scaffolding for Tier-2 files + contribution governance (templates, hooks,
CONTRIBUTING.md). `init` scaffolds the tier set you pick; all of the above is in v1.

## 2. CLI Surface (Typer)

```
proj init [--tier core] [--tier cv] [--tier infra] [--tier quality]   # scaffold dirs/templates; idempotent, never overwrites
proj adr new "Title" [-s N] [-y]       # -s superseded N, -y skip editor
proj adr list [--status Accepted|Deprecated|Superseded]
proj adr link NNNN [NNNN...]           # add cross-links (see also / superseded-by)
proj adr toc                           # regenerate .proj/toc/adr.md (generated output; user docs stay in docs/)
proj adr graph [out.mmd]               # emit mermaid graph of supersede links (default: stdout)
proj task add "Title"                  # append unchecked block to TASKLIST.md
proj task done <title>                 # verify all sub-steps checked, archive to docs/plans/YYYY-MM-title.md
proj task list
proj changelog version vX.Y.Z [--bump major|minor|patch]  # read current from CHANGELOG, create/insert section
proj changelog add "<Added|Changed|...> <text>" [--version v]
proj changelog release-notes vX.Y.Z    # print/markdown of one version's section (for PR body/tag body)
proj debt new "Title"                  # docs/tech-debt/NNNN-short-title.md
proj debt list
proj debt repay NNNN                   # mark Repaid + date
proj incident new "Title"
proj experiment new "Title" [--commit <hash>]
proj hooks install                     # install .githooks/ (pre-commit: proj check; pre-push: optional)
proj check                             # verify invariants (ADR number==heading, TASKLIST clean, changelog order); exit non-zero on failure
```

### Added design notes (expanded scope)

- **`adr graph`**: parses `Superseded by ADR-NNNN` lines only → mermaid
  `graph LR` (supersede = directed edge, links = dotted). Output to stdout or file.
- **`changelog version --bump`**: current version = first `## vX` header (or
  `pyproject.toml [project] version` if no released entry). Bump per semver;
  inserts a new `[Unreleased]`-then-`## vX.Y.Z` section on top. Never rewrites
  existing version sections.
- **git hooks**: v1 ships ONE hook — `pre-commit` running `proj check` (fast, pure
  filesystem). Hooks live under `.proj/githooks/` (committed, shareable);
  `hooks install` runs `git config core.hooksPath .proj/githooks`. No other hooks.
- **Templates for governance files**: `CONTRIBUTING.md`,
  `.github/ISSUE_TEMPLATE/bug.md` + `feature.md`, `PULL_REQUEST_TEMPLATE.md`
  carry the rule_draft checklist (ADR-first for big changes, TASKLIST rule,
  changelog rule, no-secrets rule).

- Numbering: ADR & tech-debt share the `NNNN` pattern; each directory numbers itself.
- `proj check` is the CI-able invariant validator — the differentiator vs adr-tools.

## 3. Project Layout (the tool itself)

```
pyproject.toml          # project + [tool.pixi.*] (local dev/test)
src/proj/
  __init__.py
  __main__.py           # python -m proj
  cli.py                # Typer app, subcommand wiring only
  config.py             # project-root discovery (walk up for pyproject.toml/.git dir), ProjConfig dataclass
  records.py            # generic numbered-record engine: discover, next_number, create, list, set_status
  adr.py                # ADR-specific: supersede links, toc generation, status parsing
  tasklist.py           # TASKLIST.md block parse/add/archive; docs/plans freeze
  changelog.py          # Keep-a-Changelog parse, version bump, entry insert, release-notes slice
  scaffold.py           # init: templates for dirs, Tier-2 files, .github templates, CONTRIBUTING.md
  hooks.py              # .githooks/ contents + core.hooksPath install
  templates/            # inline string templates (no jinja dependency)
tests/
  conftest.py           # tmp_path project fixture
  test_records.py test_adr.py test_tasklist.py test_changelog.py test_init.py test_check.py
```

## 4. Key Design Decisions

1. **One generic `records.py` engine** for ADR / tech-debt / experiments (all are
   "numbered markdown files with Status"). Specialization = small subclasses
   (ADR adds supersede + toc; incident/experiment use date-based or simple numbering).
   This is the whole reason to exist over shell scripts: one codebase, testable.
2. **Config lives in `pyproject.toml`** under `[tool.proj]`:
   ```toml
   [tool.proj]
   adr_dir = "docs/adr"           # defaults shown = rule_draft defaults (path uses ./docs)
   plans_dir = "docs/plans"
   changelog = "CHANGELOG.md"
   tiers = ["core", "quality"]
   ```
   Absent section → defaults. `init` writes the section in.
3. **No git dependency** in v1 (pure filesystem). `proj check` may read git later.
4. **Editor**: `adr new` opens `$VISUAL/$EDITOR` unless `--yes`; same pattern as adr-tools.
5. **Dependencies (runtime)**: `typer`, `pathlib`. That's it. No markdown lib —
   formats are simple enough for line-level parsing (keep parse tolerant, not strict).
6. **Git**: only `subprocess` calls to `git config core.hooksPath` for
   `hooks install`; no git parsing elsewhere.
7. **Semver bumping**: implemented as plain string arithmetic (no `packaging` dep).
8. **`./.proj/` = tool-owned files**: anything the tool generates/regenerates
   (hooks, TOC output) lives in `.proj/` at the project root — never mixed into
   `docs/` (user-authored) or the repo root. `init` creates `.proj/` + a `.proj/.gitignore`
   that keeps generated-only outputs out of VCS if desired (hooks ARE committed).

## 5. Distribution & Testing

- **uvx-ready**: standard `[project]` + `[project.scripts] proj = "proj.cli:app"`.
  uvx runs the entry point from a fresh venv → the tool must have *no* system
  prerequisites (typer+rich only, pure Python).
- **pixi (local testing)** in `pyproject.toml`:
  ```toml
  [tool.pixi.workspace]
  channels = ["conda-forge"]
  platforms = ["linux-64"]        # + osx-64/arm64 as needed

  [tool.pixi.pypi-dependencies]
  proj = { path = ".", editable = true }

  [tool.pixi.feature.dev.dependencies]
  pytest = "*"
  ruff = "*"

  [tool.pixi.environments]
  dev = ["dev"]
  ```
  Usage: `pixi run pytest`, `pixi run ruff check`.
  (Verify final pixi manifest keys against current pixi docs at implementation time —
  pixi config in pyproject has evolved: `dependency-groups` vs `feature`.)

## 6. Work Plan (execution order)

1. `pyproject.toml` + package skeleton + empty Typer app → verify `pixi run proj --help`
2. `config.py` (root discovery, `[tool.proj]` read) + tests
3. `records.py` engine + tests (numbering, create, list, status)
4. `adr.py` + templates; `proj adr new/list/toc` → verify against a scratch repo
5. `tasklist.py` (add/done/archive) + tests
6. `changelog.py` (version/bump/add-entry/release-notes) + tests
7. adr extras: `link`, `toc`, `graph` + tests
8. debt/incident/experiment subcommands (thin wrappers over records engine)
9. `scaffold.py` `init` + tier + governance templates + tests
10. `hooks.py` `hooks install` + pre-commit hook + test
11. `proj check` invariants + tests
12. Final: `uv tool run` / uvx-style smoke test, README
