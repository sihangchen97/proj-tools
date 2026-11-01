# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`proj` (distribution `proj-tools`, import name `proj`) is a project-governance CLI + stdio MCP
server: decisions, tasks, plans, changelog, tech debt, incidents, experiments. One generic
engine; YAML profiles are the schema, SQLite (`.proj.sqlite`) is the source of truth, Markdown
is the human/LLM input channel and rendered output.

**This repo is the tool's dev repo, not a proj project.** Never run state-changing `proj`
commands (`init`, `new`, `sync`, `render`, `update`, `<rt> <verb>`, …) at the repo root, and
never write test data into root-level `tasks/`, `docs/plans/`, `docs/decisions/`. Full-project
experiments go in the `.testproj/` sandbox (e2e) or pytest `tmp_path` (from `.roo/rules/testing.md`).

## Commands

```bash
pixi install                      # dev env
pixi run test                     # pytest -q (dev env)
pixi run -e dev pytest tests/test_task_plan.py::test_name -q   # single test
pixi run lint                     # ruff check src tests
pixi run e2e                      # bash scripts/e2e.sh: full CLI lifecycle in .testproj/ (rebuilt each run)
bash scripts/init-demo.sh         # browseable demo project in .testproj/
pixi run proj --help
hatch build                       # wheel (profiles/ is force-included as proj/profiles)
```

After changing any render/template logic, run both `pixi run test` and `pixi run e2e`. New e2e
checks go into `scripts/e2e.sh` using `expect_ok` / `expect_fail`. Pytest tests that need a full
project use the `proj` fixture in `tests/conftest.py` (inits `default` + `quality` profiles in
`tmp_path`) — don't hardcode the repo root or `chdir` into it.

`docs/CLI.md` and `docs/MCP.md` are generated from `src/proj/docs.py` + the live CLI/MCP
definitions via `proj docs` — regenerate (from a sandbox or with `--root`) rather than hand-edit.

## Architecture

Layers (`src/proj/`):

- `core/templates.py` — loads YAML profiles into `RecordTypeSpec`s (record_type, `code`, dir,
  `numbered`, `verbs` state machine, `status-allowed` with emoji/`done`, fields, sections,
  `features`, `render` toggles). Docstring documents the profile format.
- `core/db.py` — SQLite `Db` wrapper (one shared `records` table with a `type` column, plus
  `record_changelog`, `items`, and a `relations` table for supersede/related/parent edges), id
  allocation (`next_id`), and `.proj.lock` read/write (pins the templates/schema the DB was built
  from; `proj init` follows an existing lock).
- `engine/store.py` — module-level storage functions: md parsing (id marker on line 1, the
  tool-owned head fence `<!-- proj:head:start … -->` … `<!-- proj:head:end -->` with title,
  `> **key:** value` blockquote metadata and optional italic summary, rendered changelog table,
  `##` sections), ingest (md → DB) and render (DB → md). Its module docstring is the canonical
  description of the md file shape.
- `engine/record.py` — `Record` base class (new, transition via template verbs,
  `validate_transition`, `on_ingest`, `check_rows`, supersede/related links) plus the
  `@register` / `record_for` registry. Types without a subclass use the base class.
- `record_types/` — behavior subclasses only where YAML isn't enough: `task` (parent refs /
  `task move` / re-open restriction under a closed ancestor), `changelog`. The checklists
  and the `gates.tasks`/`gates.items` promotion live in `profiles/*.yaml` + `engine/items.py`,
  not in these subclasses. Importing the package registers them.
- `engine/items.py` — the structured checklists (a record's `items:` section): parse/render
  one item line (`- [ ] #NN text`), merge the md list on sync, the `item` ops (add/set/
  check/uncheck/edit/mv/rm/ls), the `gates.items` block, the closed-state re-open
  restriction, and the `n/m` progress suffix on task lines.
- `engine/views.py` — generated views (`TASKLIST.md`, plan task trees, `CHANGELOG.md`).
- `cli.py` — Typer app; per-record_type subcommands (`new/ls/show/update/<verb>`) are built
  dynamically from the registry, so a new record_type needs only a YAML file; plus cross-type
  `proj show/ls/open ID` (the id carries its type) and the static `item` group
  (`proj item ls/add/set/check/uncheck/edit/mv/rm/group`).
- `mcp.py` — `proj-mcp` stdio server exposing the same engine functions (one tool per CLI verb,
  optional `root`, falls back to `$PROJ_HOME`); includes the `proj_item_*` tools.
- `check.py` (invariants: md↔DB, filename↔id, schema↔template, nesting, relation consistency),
  `scaffold.py` (`init`), `hooks.py` (pre-commit → `proj check`), `docs.py`.

Key invariants:
- Sync conflict ownership: `##` section content → md wins; id/uuid/title/status/fields/changelog → DB wins.
- Rendered views and the head fence are never hand-edited; change them via `proj`.
- Bundled profiles live in repo-root `profiles/` (`default/*.yaml`, plus top-level `task.yaml`,
  `changelog.yaml`, `texts.yaml` — the latter holds all human-facing text written into projects).
  A project's own `profiles/<record_type>.yaml` overrides the bundled one.

## Terminology (single vocabulary — do not mix in)

One canonical name per concept; use it in code, CLI output and MCP:

| term | meaning | not to confuse with |
|---|---|---|
| **record** | one governed object = one md file + one DB row (`records` table, `type` column) | a DB table, a template |
| **record type** | the registered kind (task, decision, plan, …), defined by one YAML profile | `kind` (a section's text-kind or a relations-edge kind — internal, not the same concept) |
| **id** | `{CODE}-{digits}` (e.g. `DEC-0001`, `T-000001`); the digits carry no letters, the code carries the type — a bare/shortened id resolves across types | a uuid, a filename (slug is derived from the title) |
| **status** | the record's state in the template's state machine (`status-allowed`, one display name each) | a verb (which changes it), a gate (which must pass) |
| **verb** | a template-declared state transition (`from` → `to`), run as `proj <type> <verb> ID` / `proj_verb` | a CLI subcommand name in general |
| **gate** | a status that requires a check before entering (`gates.tasks` = all descendant tasks terminal; `gates.items` = all checklist items closed) | a status itself |
| **item** | one line of a record's structured checklist (the `items:` section), ref `ID#nnn` (`#A01` = group A) | a checkbox in a task's Steps, a section |
| **group** | a lettered (A–Z) sub-list inside an item checklist | a directory, a plan |
| **WIP** | `WIP.md` — the work-in-progress list any record type can join (phase 3; replaces TASKLIST.md) | TASKLIST.md (removed) |
| **summary** | the record's one-sentence line (DB column `summary`); the `--summary`/`-s` option | `one_sentence` (old name, gone) |
| **changelog** | the per-record log (rows in `record_changelog`) + the project's `CHANGELOG.md` release notes (the `changelog` record type) | a record's status history (same rows, shown via `--history`) |

Language (Q7): tool output, md structure and profile hints are English; docs and
design plans are Chinese; the e2e demo project's prose is Chinese.

## Notes

- The schema is in flux: `docs/unified-schema-plan.md` is an in-progress plan (unified `records`
  table, `parent` as a relation, `features:` → `contain_task`). The README's id/status tables can
  lag behind the code — trust `profiles/*.yaml` and the module docstrings.
- Dev stage: no data migrations or backward compatibility for old `.proj.sqlite` files.
- `plans/` (local design docs, e.g. `plans/PLAN.md`), `tobedelete/` (staging area for removed
  files — move rather than delete), `reference/`, `.roo/` are gitignored.
- Ruff: line length 100, py310 target; `docs.py` intentionally contains Chinese prose.
  Much of the comments/docs are in Chinese.
