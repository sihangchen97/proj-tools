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
proj init --profile default          # additive: add more with --profile quality …
proj decision new "Use pixi" -y     # docs/decisions/DEC-0002-*.md (the -y skips the editor)
proj task new "Data pipeline" -y    # docs/tasks/T-000001-*.md (status: Draft)
proj plan new "Ship v1" -y
proj task new "Baseline" --parent PLN-0001   # drawn in the plan's Tasks block
proj plan activate PLN-0001      # Active → auto-registered in WIP.md
proj item add T-000001 "tests pass" --owner alex
proj item check T-000001#001     # first closed criterion → Draft auto-promotes to
                                 # In Progress (the task joins WIP.md)
proj item edit T-000001#001 --owner billy --reassign   # taking over an owned item
proj task done T-000001          # needs every criterion closed + sub-tasks terminal
proj plan review PLN-0001 && proj plan shelve PLN-0001
proj changelog add "Added: sqlite core"
proj changelog version --bump patch
proj check                       # invariants; also runs in the pre-commit hook
```

`--root PATH` works on every command (run from anywhere, e.g. for MCP use).

## Core model

- **Source of truth = `.proj.sqlite`** (project root — one table per record_type
  + the shared `record_changelog`; committed by default).
  The md files are **input channel + rendered output**: edit `##` section
  content → `proj sync` reads it back, then re-renders every md from the DB.
- **Templates are YAML schema** (bundled with the package; the project's own
  `profiles/<record_type>.yaml` overrides them per record_type). A template
  declares: record_type, dir, numbering, allowed statuses, verbs (state
  machine), sections (+ hints), a structured checklist (`items`), gates.
  New record_type = one YAML file, zero code changes.
- **`.proj.lock`** (committed) pins which templates + schema the DB was built
  from. `proj init` with a lock follows the lock; without, it builds from the
  bundled profiles and writes the lock.
- **IDs live in the filename and the md** (e.g. `T-000001`); each md carries
  an invisible first line `<!-- proj:<record_type> <id> -->` for cross-validation
  (the id must match the filename prefix).
- **Rendered views, never hand-edit**: `WIP.md` (the item lines in it ARE yours
  to tick — `proj sync` writes them back into the record), `CHANGELOG.md`, and
  each record's head fence (metadata/summary/changelog) and `Tasks` block.
- **Ownership rules on conflict** (sync): section content + title → md wins
  (a changed title renames the file to the new slug);
  id/created/status/fields/changelog → DB wins.
- **Multi-writer safety**: every write command holds the project write lock
  (`.proj.write-lock`, flock) and all md files are written atomically, so
  several agents / terminals can share one project directory — they queue,
  and a crashed command never leaves a half-written file.

## Kinds

| record_type | dir | id | statuses |
|---|---|---|---|
| `decision` | `docs/decisions/` | DEC-NNNN | Draft, Proposed, Accepted, Rejected, Deprecated (supersede = fields, see below) |
| `task` | `docs/tasks/` | T-NNNNNN | Draft, In Progress, Failed, Done |
| `plan` | `docs/plans/` | PLN-NNNN | Draft, Active, Pending Review, Shelved, Archived |
| `debt` | `docs/debts/` | DEB-NNNN | Draft, Open, In Progress, Repaid, Waived |
| `incident` | `docs/incidents/` | INC-YYYY-MM-DD | Draft, Open, Mitigated, Closed |
| `experiment` | `docs/experiments/` | EXP-NNNN | Draft, Proposed, Running, Closed, Abandoned |

Statuses show an emoji in views (`proj ls`, WIP.md) when the template defines
one; the md `Status:` line stays plain text.

> The plan↔task relationship and the checklist model are about to change
> (multi-checklist support) — see `plans/PLAN.md` §10. The commands in the
> quick start above reflect the current behavior.

### Decisions supersede (multi-valued)

Supersede is a relationship, not a status: one decision can replace several
old ones, and one old one can be replaced by several new ones.

- `proj decision new TITLE --supersede NNNN` (repeatable; `NNNN,MMMM` also
  works) sets `supersedes` on the new record and appends to each old record's
  `superseded_by`.
- `proj check` enforces: no dangling refs, A↔B mutual consistency, no cycles.

### Tasks link to any record via `parent`

Tasks are independent records; `task --parent` (a relation, any record type)
links them to a plan, a parent task (sub-task), or an experiment. No file
moving; children are resolved at render time, any depth, drawn in the parent's
`Tasks` block (e.g. the plan's md shows its task tree).

- **WIP.md membership is derived from status, not from the link**: a record
  joins WIP.md when it switches into a status listed in its type's
  `wip.enter` (a task's In Progress, a plan's Active) and leaves when it
  moves out. `proj wip add` / `proj wip return` / `proj wip ls` manage it
  explicitly.
- A task without a parent is just an independent entry (docs call the
  grouping *Unparented*); `proj task move T-… --parent P` re-homes it and
  `--detach` removes the edge.

Plan state machine: `Draft →(activate) Active →(review: all its tasks Done)
Pending Review →(shelve) Shelved →(resume) Active`;
`Pending Review →(archive) Archived` and `Shelved →(freeze) Archived`.

### Task criteria (checklist)

```markdown
## Acceptance Criteria
- [x] `pixi run test` passes
- [ ] deploy script · owner: alex · due: 2026-12-01
```

- Numbers (`#001`, `#A01` in a group) are assigned by the tool; states are
  `open` / `done` / `failed` / `dropped` (`[x]` is done, `[x] ❌` failed, …).
- First closed criterion → task auto-promotes Draft → In Progress.
- `proj item add|check|uncheck|set|edit|mv|rm T-…#NNN`, `proj item ls`
  (filter `--owner`, `--open`, …).
- Taking an owned item over needs `--reassign` (`--owner <you> --reassign`);
  the hand-over is logged in the record's changelog.
- `proj task done` → refuses while any criterion is still open, or while any
  sub-task in the tree is not terminal (Done/Failed).

## CLI

```
proj init [--profile X] [--root P]
proj sync [FILE…]          # md → DB → md (views re-rendered too)
proj adopt [FILE…]         # absorb pre-existing md
proj render [record_type [ID]]    # DB → md only
proj check                 # md↔DB, filename↔id, schema↔template, refs
proj info [--json]         # what this dir manages + ops cheatsheet
proj show ID / proj ls [--status S] / proj open ID
proj docs                  # regenerate docs/CLI.md + docs/MCP.md
proj changelog add|version|release-notes|list
proj hooks install         # pre-commit → proj check
proj bug "…"              # record a proj bug in .proj.bugs (never committed)

# per record_type (dynamic; <verb> = one from that type's template `verbs:`)
proj <rt> new TITLE [--supersede N] [--parent N] [-y]
proj <rt> list [--status S]
proj <rt> show ID [--history] [--json]
proj <rt> update ID [-t …] [-s …] [-m "Category: …"]   # title / summary / changelog
proj <rt> <verb> ID        # state-machine step, e.g. decision accept, plan activate

# checklist items (T-…#NNN; group items T-…#A01)
proj item ls|add|set|check|uncheck|edit|mv|rm

# WIP (the "what is in progress" list)
proj wip add|return|ls
```

> 完整参考：[`docs/CLI.md`](docs/CLI.md)（`proj docs` 从代码生成，永不漂移）。

## MCP

`proj-mcp` ships a stdio MCP server (`mcp` 2.x) exposing the engine as tools —
they cover the same **capabilities** as the CLI, but they are not a
one-to-one mapping of commands. Every tool takes an optional `root`
(absolute project path, = the CLI `--root`) so it works from any cwd; the
server also reads `PROJ_HOME` and falls back to its working directory:

```json
{
 "mcpServers": {
   "proj": { "command": "proj-mcp",
             "env": { "PROJ_AGENT": "claude" } }
 }
}
```

Highlights (full list in `docs/MCP.md`):

| tool | equivalent CLI |
|---|---|
| `proj_init` | `proj init --profile X` |
| `proj_new` | `proj <rt> new TITLE [--supersede …] [--parent N]` |
| `proj_sync` / `proj_adopt` / `proj_render` | `proj sync` / `adopt` / `render` |
| `proj_check` / `proj_info` | `proj check` / `proj info` |
| `proj_show` / `proj_ls` | `proj show ID [--history]` / `proj ls` |
| `proj_update` | `proj <rt> update ID` (title / summary / changelog entry) |
| `proj_verb` | `proj <rt> <verb> ID` (template verb: `decision accept`, `plan activate`, `task done`, …) |
| `proj_task_move` | `proj task move T-… --parent P` / `--detach` |
| `proj_item_ls` / `proj_item_add` / `proj_item_update` / `proj_item_rm` | `proj item …` (checklist ops, `--reassign` included) |
| `proj_wip_add` / `proj_wip_ls` / `proj_wip_return` | `proj wip …` |
| `proj_changelog_add` / `_version` / `_release_notes` / `_list` | `proj changelog …` |
| `proj_bug` / `proj_bug_ls` | `proj bug "…"` (records tool bugs in `.proj.bugs`) |

Tools return human-readable text; on validation failure the full error message
is returned (agent-readable).

## Adding a record_type

Drop `profiles/default/<record_type>.yaml` (see existing ones for the format), then
`proj init` (or `proj render` after `proj sync` if tables exist). That's it —
the engine is fully generic.

## Scope & known limits

- **Multiple agents / terminals in one project directory: supported.** Every
  write command holds the project write lock and waits (up to 10 s) for the
  current writer, then proceeds; md files are written atomically, so no reader
  ever sees a half-written file.
- **Multiple people in separate git clones: not supported yet.** Ids would
  collide on merge and `.proj.sqlite` cannot be merged. Git-based collaboration
  is post-release work (md as the mergeable source of truth — see
  `plans/PLAN.md` §S.5).
- **Network filesystems (NFS/CIFS):** SQLite WAL is unsafe there, so proj
  detects the filesystem and falls back to `journal_mode=DELETE`
  (`proj info` shows the mode and a hint). Avoid writing the same project from
  several machines at once.
- **Who made a change:** set `PROJ_AGENT` (e.g. `PROJ_AGENT=claude proj …`, or
  `env` in the MCP server entry above) — every changelog entry records the
  actor; unset falls back to the system username. Also use it as the name you
  claim checklist items with (`--owner <you>`).
- **Tool bugs:** `proj bug "what happened"` (or the MCP `proj_bug` tool)
  appends to `.proj.bugs` in the project (never committed, `gitignore`d) —
  auto-recorded with the traceback on unexpected errors too.
- **Performance:** commands are single-transaction by design; to speed up
  startup, install the pixi/uv environment on local disk rather than on the
  network mount (see `plans/PLAN.md` §13).

## Tests / lint

```bash
pixi run test        # pytest (temp-dir sandboxes)
pixi run lint        # ruff
pixi run e2e         # full-lifecycle smoke (`.testproj` sandbox, rebuilt each run)
pixi run e2e-full    # e2e + the generated scripts/e2e.sh equivalence check
```
