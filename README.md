# proj

Project governance CLI: decisions (ADR), tasks, plans, changelog, tech debt,
incidents, experiments. One generic engine, YAML templates as schema,
SQLite as source of truth, Markdown as the human/LLM input channel.

Design doc (kept outside the git repo, in `plans/` locally):
`plans/PLAN.md` — architecture, record types, CLI surface, release state and
the open work list.

## Install / develop

```bash
pixi install            # dev environment (project root)
pixi run proj --help    # the CLI itself
```

`pyproject.toml` registers the `proj` and `proj-mcp` entry points. Not on PyPI
yet (in development) — install a wheel into any environment:

```bash
hatch build                    # in this repo (or: python -m build)
pip install dist/proj-*.whl   # installs `proj` + `proj-mcp`
```

## Quick start

```bash
cd my-project
proj init --profile core            # + --profile cv|infra|quality
proj decision new "Use pixi"        # opens $EDITOR on docs/decisions/DEC-0002-*.md
proj sync                           # after editing md: md → DB → re-render md
proj task new "Data pipeline"       # docs/tasks/T-000001-*.md
proj plan new "Ship v1"
proj task new "Baseline" --parent 1   # attaches to the plan (its Task List)
proj plan activate 1                # its tasks appear in TASKLIST.md
# ... edit steps, check boxes ...
proj sync
proj task done A                    # requires every step + sub-task checked
proj plan review 1 && proj plan shelve 1
proj changelog add "Added: sqlite core"
proj changelog version --bump minor
proj check                          # invariants; also runs in pre-commit hook
```

`--root PATH` works on every command (run from anywhere, e.g. for MCP use).

## Core model

- **Source of truth = `.proj.sqlite`** (project root — one table per record_type +
  one changelog table per record_type; committed by default).
  The md files are **input channel + rendered output**: edit `##` section
  content → `proj sync` reads it back, then re-renders every md from the DB.
- **Templates are YAML schema** (bundled with the package; the project's own
  `profiles/<record_type>.yaml` overrides them per record_type). A template
  declares: record_type, dir, numbering, allowed statuses, extra fields,
  sections (+ hints). New record_type = one YAML file, zero code changes.
- **`.proj.lock`** (committed) pins which templates + schema the DB was built
  from. `proj init` with a lock follows the lock; without, it builds from the
  bundled profiles and writes the lock.
- **UUIDs live only in the DB**; each md carries them as an invisible first
  line `<!-- proj:<record_type> <uuid> -->` for cross-validation (uuid → id must
  match the filename prefix).
- **Rendered views, never hand-edit**: `TASKLIST.md`, `CHANGELOG.md`, the
  `Task List` section of plans.
- **Ownership rules on conflict** (sync): section content + title → md wins
  (a changed title renames the file to the new slug);
  id/uuid/created/status/fields/changelog → DB wins.

## Kinds

| record_type | dir | id | statuses |
|---|---|---|---|
| `decision` | `docs/decisions/` | NNNN | Proposed, Accepted, Deprecated (supersede = fields, see below) |
| `task` | `tasks/` | A, B, … AA | Not started, In progress, Done |
| `plan` | `docs/plans/` | NNNN | Draft, Active, Pending review, Shelved, Archived |
| `debt` | `docs/debts/` | NNNN | Open, In progress, Repaid |
| `incident` | `docs/incidents/` | YYYY-MM-DD | Open, Closed |
| `experiment` | `docs/experiments/` | NNNN | Proposed, Running, Closed |

Statuses show an emoji in views (`_TOC.md`, `proj list`) when the template
defines one; the md `Status:` line stays plain text.

### Decisions supersede (multi-valued)

Supersede is a relationship, not a status: one decision can replace several
old ones, and one old one can be replaced by several new ones.

- `proj decision new TITLE -s 1 -s 2` (repeatable; `-s "1,2"` also works)
  sets `supersedes` on the new record and appends to each old record's
  `superseded_by`.
- `proj check` enforces: no dangling refs, A↔B mutual consistency, no cycles.

### Tasks link to any record via `parent`

Tasks are independent records; `task.parent` (a template field, any record type)
links them to a plan, a parent task (sub-task), or an experiment. No file
moving; children are resolved at render time.

- `parent` empty → task in the global `TASKLIST.md`
- `parent` = an **Active** plan → task also promoted into `TASKLIST.md`, and
  drawn in that plan's `Task List` section with its sub-task tree
- `parent` = any other record → task parked in that record's `Task List` section

State machine: `Draft →(activate) Active →(review: all tasks Done)
Pending review →(shelve) Shelved →(resume) Active`;
`Pending review →(archive) Archived` and `Shelved →(freeze) Archived`.

### Task Steps (two-level checkboxes)

```markdown
## Steps
- [x] step one
  - [ ] sub-task
- [ ] step two
```

- First `[x]` anywhere → auto `Not started → In progress` (on sync).
- `proj task done A` → requires **all** checkboxes checked.
- Level-3 nesting → `proj check` failure (flatten or split into a new task).

## CLI

```
proj init [--profile X] [--root P]
proj sync [FILE…]          # md → DB → md
proj adopt [FILE…]         # absorb pre-existing md (generates uuids)
proj render [record_type [ID]]    # DB → md only
proj update                # sync + all views
proj check                 # md↔DB, filename↔id, schema↔template, nesting
proj info [--json]         # what this dir manages + ops cheatsheet
proj docs                  # regenerate docs/CLI.md + docs/MCP.md
proj changelog add|version|release-notes|list
proj hooks install         # pre-commit → proj check

# per record_type (dynamic; <verb> = one from that type's template `verbs:`)
proj <rt> new TITLE [-s N] [--parent N] [--field k=v] [-y]
proj <rt> list [--status S]
proj <rt> show ID [--history]
proj <rt> <verb> ID        # state-machine step, e.g. decision accept, plan activate
```

> 完整参考：[`docs/CLI.md`](docs/CLI.md)（`proj docs` 从代码生成，永不漂移）。

## MCP

`proj-mcp` ships a stdio MCP server (`mcp` 2.x, `MCPServer`) exposing the same
engine functions as the CLI — one tool per CLI verb, each with an optional `root`
(absolute project path, = the CLI `--root`) so it works from any cwd:

```json
{
 "mcpServers": {
   "proj": { "command": "proj-mcp" }
 }
}
```

| tool | equivalent CLI |
|---|---|
| `proj_init` | `proj init --profile X` |
| `proj_new` | `proj TYPE new TITLE [-s …] [--parent N] [--field k=v]` |
| `proj_sync` | `proj sync [FILE…]` |
| `proj_check` | `proj check` |
| `proj_show` | `proj TYPE show ID [--history]` |
| `proj_verb` | `proj TYPE <verb> ID` (template verb: `decision accept`, `plan activate`, `task done`, …) |
| `proj_update_record` | `proj TYPE update ID [-t …] [-s …] [-c "…"]` |
| `proj_update` | `proj update` (global sync + render, not per-record) |
| `proj_info` | `proj info [--json]` |
| `proj_changelog_add` | `proj changelog add "…"` |

Tools return human-readable text; on validation failure the full error message is
returned (agent-readable).

The server inherits the client's working directory — if the client runs inside
your project (e.g. Claude Code from the repo), no config is needed. From a
global config (client cwd ≠ project), pass `root` (absolute project path) to
every tool call, or set `"cwd"` in the server entry where your client supports it.

## Adding a record_type

Drop `profiles/default/<record_type>.yaml` (see existing ones for the format), then
`proj init` (or `proj render` after `proj sync` if tables exist). That's it —
the engine is fully generic.

## Tests / lint

```bash
pixi run test        # pytest
pixi run lint        # ruff
pixi run e2e         # bash full-lifecycle smoke (.testproj sandbox, rebuilt each run)
```
