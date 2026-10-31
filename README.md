# proj

Project governance CLI: decisions (ADR), tasks, plans, changelog, tech debt,
incidents, experiments. One generic engine, YAML templates as schema,
SQLite as source of truth, Markdown as the human/LLM input channel.

Design docs (kept outside the git repo, in `plans/` locally):
`v3-sqlite-core.md` (current), `v2-templates-and-index.md` (superseded core
premise), `architecture.md` (v1). Implementation deviations:
`v3-implementation-notes.md`.

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
proj new decision "Use pixi"        # opens $EDITOR on docs/decisions/0001-*.md
proj sync                           # after editing md: md → DB → re-render md
proj new task "Data pipeline"       # tasks/A-data-pipeline.md
proj new plan "Ship v1"
proj new task "Baseline" --parent 1   # attaches to the plan (its Task List)
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
- **Templates are YAML schema** (`profiles/default/<record_type>.yaml`, copied into
  the project at init, user-editable). A template declares: record_type, dir,
  numbering, allowed statuses, extra fields, sections (+ hints). New record_type =
  drop one YAML file, zero code changes.
- **`<record_type>.lock` = `.proj.lock`** (committed) pins which templates + schema
  the DB was built from. `proj init` with a lock follows the lock; without,
  it builds from `profiles/` and writes the lock.
- **UUIDs live only in the DB**; each md carries them as an invisible first
  line `<!-- proj:<record_type> <uuid> -->` for cross-validation (uuid → id must
  match the filename prefix).
- **Rendered views, never hand-edit**: `TASKLIST.md`, `CHANGELOG.md`, the
  `Task List` section of plans.
- **Ownership rules on conflict** (sync): section content → md wins;
  id/uuid/created/title/status/fields/changelog → DB wins.

## Kinds

| record_type | dir | id | statuses |
|---|---|---|---|
| `decision` | `docs/decisions/` | NNNN | Proposed, Accepted, Deprecated (supersede = fields, see below) |
| `task` | `tasks/` | A, B, … AA | Not started, In progress, Done |
| `plan` | `docs/plans/` | NNNN | Draft, Active, Pending review, Shelved, Archived |
| `debt` | `docs/tech-debt/` | NNNN | Open, In progress, Repaid |
| `incident` | `docs/incidents/` | YYYY-MM-DD | Open, Closed |
| `experiment` | `docs/experiments/` | NNNN | Proposed, Running, Closed |

Statuses show an emoji in views (`_TOC.md`, `proj list`) when the template
defines one; the md `Status:` line stays plain text.

### Decisions supersede (multi-valued)

Supersede is a relationship, not a status: one decision can replace several
old ones, and one old one can be replaced by several new ones.

- `proj new decision TITLE -s 1 -s 2` (repeatable; `-s "1,2"` also works)
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
proj new <record_type> TITLE [-s N] [--parent N] [--field k=v] [-y]
proj sync [FILE…]          # md → DB → md
proj adopt [FILE…]         # absorb pre-existing md (generates uuids)
proj render [record_type [ID]]    # DB → md only
proj update                # sync + all views
proj list <record_type> [--status S]
proj show <record_type> ID [--history]
proj status <record_type> ID STATUS   # validated; auto changelog entry
proj log <record_type> ID "Category: …"
proj check                 # md↔DB, filename↔id, schema↔template, nesting
proj info [--json]         # what this dir manages + ops cheatsheet
proj decision toc|graph|link
proj task new|done|list
proj plan new|activate|review|shelve|resume|archive|list
proj changelog add|version|release-notes|list
proj hooks install         # pre-commit → proj check
```

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
| `proj_new` | `proj new TYPE TITLE [-s …] [--parent N] [--field k=v]` |
| `proj_sync` | `proj sync [FILE…]` |
| `proj_check` | `proj check` |
| `proj_list` | `proj list TYPE [--status S]` |
| `proj_show` | `proj show TYPE ID [--history]` |
| `proj_status` | `proj status TYPE ID STATUS` |
| `proj_log` | `proj log TYPE ID "…"` |
| `proj_update` | `proj update` |
| `proj_info` | `proj info [--json]` |
| `proj_task_done` | `proj task done ID` |
| `proj_plan_verb` | `proj plan activate|review|shelve|resume|archive|freeze ID` |
| `proj_decision_supersede` | `proj decision new TITLE -s …` |
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
