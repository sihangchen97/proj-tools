# AGENTS.md

This file provides guidance to agents when working with code in this repository.

`proj` (distribution `proj-tools`, import name `proj`; two CLIs: `proj`, `proj-mcp`) is a
project-governance CLI + stdio MCP server: decisions, tasks, plans, changelog, debt, incidents.
One generic engine; YAML profiles (`profiles/*.yaml`) are the schema, SQLite is source of truth,
Markdown is the human/LLM input channel and rendered output. See `CLAUDE.md` (long form).

## This is the tool's dev repo — NOT a proj project (critical)

- Never run state-changing `proj` commands (`init`, `new`, `sync`, `render`, `update`,
  `<rt> <verb>`, …) at the repo root, and never write test data into root `tasks/`,
  `docs/plans/`, `docs/decisions/`. Full-project experiments go in the `.testproj/`
  sandbox (`pixi run e2e`) or pytest `tmp_path` (the `proj` fixture in `tests/conftest.py`).

## Commands (all via pixi, dev env)

```bash
pixi install                                   # dev env
pixi run test                                  # pytest -q -n auto (tests/)
pixi run -e dev pytest tests/test_task_plan.py::test_name -q   # single test
pixi run lint                                  # ruff check src tests
pixi run e2e                                   # scripts/e2e.py, in-process story; refreshes scripts/e2e.sh
pixi run e2e-full                              # same story as real `proj` subprocesses + equivalence check
pixi run docs                                  # regenerate docs/CLI.md + docs/MCP.md + templates/PROJ_*.md
hatch build                                    # wheel (profiles/ + templates/ force-included into the wheel)
```

- After changing ANY render/template logic: run BOTH `pixi run test` and `pixi run e2e`.
- After changing `cli.py` / `mcp.py` / `docs.py`: run `pixi run docs` — the four generated files
  (`docs/CLI.md`, `docs/MCP.md`, `templates/PROJ_CLI.md`, `templates/PROJ_MCP.md`) and
  `scripts/e2e.sh` are **never hand-edited**; on a rebase conflict, regenerate, don't hand-merge.

## Architecture (src/proj/)

- `core/config.py` — `ProjConfig` (root resolution: `--root` > `$PROJ_HOME` > cwd) + `reporter()`
  (changelog actor: `PROJ_AGENT` env, else system username).
- `core/templates.py` — YAML → `RecordTypeSpec` (code, dir, numbered, verbs state machine,
  status-allowed, fields, sections). Docstring documents the profile format.
- `core/db.py` — SQLite `Db`; ONE shared `records` table with a `type` column (not per-type
  tables), plus `record_changelog` (has an `actor` column), `items`, `relations` (supersede/
  related/parent edges); id allocation; reads/writes `.proj/lock.yaml` (pins the schema the DB
  was built from; `proj init` follows an existing lock).
- `core/fsio.py` — multi-writer safety: `write_atomic` (temp + `os.replace`, refuses to clobber
  a file whose mtime changed mid-command) and `project_lock` (exclusive `fcntl.flock` on
  `.proj/write.lock`, re-entrant, 10 s wait). EVERY write command holds the lock.
- `engine/store.py` — module-level storage (NOT a class): md parsing/ingest/render. Its module
  docstring is the canonical description of the md file shape (id marker line 1, the
  `<!-- proj:head:start … -->`…`<!-- proj:head:end -->` fence = tool-owned, `> **key:**` block,
  italic summary, changelog, `##` user-owned sections).
- `engine/record.py` — `Record` base + `@register`/`record_for`; types with no subclass use base.
- `record_types/` — behavior subclasses only where YAML isn't enough (`task`, `changelog`);
  checklists + gates live in `profiles/*.yaml` + `engine/items.py`, not here. Importing registers.
- `engine/items.py` — structured checklists (`items:` section, `- [ ] #NN`), transforms + n/m.
- `engine/item_ops.py` — `item` command entry points for CLI + MCP (resolve, ingest unsynced,
  transform, commit+re-render, owner hand-over).
- `engine/wip.py`, `engine/present.py` (shared CLI+MCP formatting: `show`/`ls` identical),
  `engine/views.py` (generated `WIP.md`/`CHANGELOG.md`/Tasks block), `cli.py` (Typer; per-type
  subcommands built dynamically from the registry — a new type = one YAML), `mcp.py` (stdio
  server; tools are NOT a 1:1 map of CLI commands), `bugs.py`, `check.py`, `scaffold.py`,
  `hooks.py` (pre-commit → `proj check`), `docs.py`.

## Invariants / conventions

- Sync conflict ownership: `##` section content → **md wins**; id/title/status/fields/changelog
  → **DB wins**. Rendered views + the head fence are never hand-edited — change them via `proj`.
- The committed source of truth is `<root>/.proj/events.jsonl`; the DB is
  `<root>/.proj/cache.sqlite` (a local cache rebuilt from the log, never
  committed — `config.db_path`).
- No data migrations / no backward compat for old DBs (dev stage): bump `SCHEMA_VERSION`, fresh
  `init` picks it up.
- New record type = add a YAML profile (subcommand builds automatically); only add a
  `record_types/` subclass when YAML can't express the behavior.
- Bundled profiles live in repo-root `profiles/` (`default/*.yaml`, top-level `task.yaml`,
  `changelog.yaml`, `texts.yaml`); a project's own `profiles/<type>.yaml` overrides the bundled.
- Language: tool output / md structure / profile hints = English; docs & design plans = Chinese;
  the e2e demo project's prose = Chinese. Much of the code comments/docs are in Chinese.

## Code style (ruff)

- `line-length = 100`, `target-version = py310`; rules `E,F,I,UP,B,SIM,RUF,PLW`.
- `from __future__ import annotations` at top of modules; type-hint public functions.
- `docs.py` + `scripts/e2e.py` are exempt from RUF001/002/003/005 (Chinese fullwidth punctuation
  is intentional — do not "fix" it).
- Module-level functions in `engine/store.py`; the `Record` class hierarchy lives in `record.py`
  and is referenced lazily (avoids import cycles).

## Testing

- pytest `tmp_path`-based; use the `proj` fixture (inits `default` + `quality` profiles in a
  throwaway dir). Don't hardcode the repo root or `chdir` into tests.
- Autouse `_render_verify` fixture: after EVERY test the on-disk md must equal a full in-memory
  render of the DB (incremental `render_affected` == `render_all`). Mark a test
  `@pytest.mark.render_drift` only when it deliberately leaves the disk drifted (the `proj check`
  drift tests).
- Bulk project builds: do NOT loop `rec.new_record` (each call auto-syncs + full `render_all`,
  O(N²)); insert rows directly through `db` in one transaction (see `many_tasks` fixture).
