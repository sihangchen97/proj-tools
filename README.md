# proj

Project governance CLI: decisions (ADR), tasks, plans, changelog, tech debt,
incidents, experiments. One generic engine, YAML templates as schema, an
append-only event log as source of truth (a SQLite cache derived by replay),
Markdown as the human/LLM input channel.

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
proj item add PLN-0001 "Baseline" --task T-000001
                                       # the plan's Work checklist references the
                                       # task (→ T-000001); ticks itself when the
                                       # task ends (Done → done, Failed → failed)
proj plan activate PLN-0001      # Active → auto-registered in WIP.md
proj item add T-000001 "tests pass" --owner alex
proj item check T-000001#001     # first closed criterion → Draft auto-promotes to
                                  # In Progress (the task joins WIP.md)
proj item edit T-000001#001 --owner billy --reassign   # taking over an owned item
proj task done T-000001          # needs every criterion closed + sub-tasks terminal
proj plan review PLN-0001        # Active → Pending Review (no task gate)
proj plan archive PLN-0001       # only once Work + Success Criteria are all closed
proj changelog add "Added: sqlite core"
proj changelog version --bump patch
proj check                       # invariants; also runs in the pre-commit hook
```

`--root PATH` works on every command (run from anywhere, e.g. for MCP use).

## Core model

- **Source of truth = `.proj/events.jsonl`** (the append-only event log; the
  whole project state is a deterministic replay of it — see "Collaborating over
  git" below). **`.proj/cache.sqlite`** (one table per record_type + the shared
  `record_changelog`) is a *derived local cache* that any command rebuilds by
  replay when missing — git-ignored.
  The md files are **input channel + rendered output**: edit `##` section
  content → `proj sync` reads it back, then re-renders every md from the DB.
- **Templates are YAML schema** (bundled with the package; the project's own
  `profiles/<record_type>.yaml` overrides them per record_type). A template
  declares: record_type, dir, numbering, allowed statuses, verbs (state
  machine), sections (+ hints), one or more structured checklists (`items`,
  each bound to a `##` section — one per type with `wip: true`, editable in
  WIP.md), gates.
  New record_type = one YAML file, zero code changes.
- **`.proj/lock.yaml`** (committed) pins which templates + schema the DB was built
  from. `proj init` with a lock follows the lock; without, it builds from the
  bundled profiles and writes the lock.
- **The `.proj/` directory** (S.10) holds all project data, keeping the repo root
  clean: `lock.yaml` (the template/schema pin) + `events.jsonl` (the event log)
  are committed and are the source of truth; `cache.sqlite` (the derived cache)
  and `write.lock` / `bugs.log` (runtime) are never committed (git-ignored by
  `.proj/.gitignore`) — a fresh clone rebuilds the cache on its first command.
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
  (`.proj/write.lock`, flock) and all md files are written atomically, so
  several agents / terminals can share one project directory — they queue,
  and a crashed command never leaves a half-written file.

## Kinds

| record_type | dir | id | statuses |
|---|---|---|---|
| `decision` | `docs/decisions/` | DEC-NNNN | Draft, Proposed, Accepted, Rejected, Deprecated (supersede is a relation, see below) |
| `task` | `docs/tasks/` | T-NNNNNN | Draft, In Progress, Failed, Done |
| `plan` | `docs/plans/` | PLN-NNNN | Draft, Active, Pending Review, Shelved, Archived |
| `debt` | `docs/debts/` | DEB-NNNN | Draft, Open, In Progress, Repaid, Waived |
| `incident` | `docs/incidents/` | INC-YYYY-MM-DD | Draft, Open, Mitigated, Closed |
| `experiment` | `docs/experiments/` | EXP-NNNN | Draft, Proposed, Running, Closed, Abandoned |

Statuses show an emoji in views (`proj ls`, WIP.md) when the template defines
one; the md `Status:` line stays plain text.

### Decisions supersede (multi-valued)

Supersede is a relationship, not a status: one decision can replace several
old ones, and one old one can be replaced by several new ones.

- `proj decision new TITLE --supersede NNNN` (repeatable; `NNNN,MMMM` also
  works) sets `supersedes` on the new record and appends to each old record's
  `superseded_by`.
- `proj check` enforces: no dangling refs, A↔B mutual consistency, no cycles.

### Tasks form a tree; plans track work via a checklist

Tasks are independent records; `task --parent` (a relation) links a task to
**another task only** (sub-task, any depth) — pointing it at a plan/debt is
refused (the CLI says: reference that plan from one of ITS checklists instead).
A task's sub-tasks render in its `## Tasks` block (depth-indented, any depth,
completed ones kept), and the task's head shows a `parent:` link.

A **plan tracks work through its `Work` checklist** (not a parent edge):
`proj item add <plan> "…" --task <T-…>` makes a checklist line reference a
task (rendered `→ T-…`). The line ticks itself when the referenced task ends
(Done → done, Failed → failed) and cannot be closed manually while the task
is still open; a bare id written in a section body (e.g. `见 DEC-0001`) is
only link-rendered, with no state coupling. Every record whose checklist
references a task is listed in that task's head as `picked by:`.

- **WIP.md membership is derived from status, not from the link**: a record
  joins WIP.md when it switches into a status listed in its type's
  `wip.enter` (a task's In Progress, a plan's Active) and leaves when it
  moves out. `proj wip add` / `proj wip return` / `proj wip ls` manage it
  explicitly.
- A task without a parent is just an independent entry (the CLI reports it as
  *Unparented*); `proj task move T-… --parent P` re-homes it and `--detach`
  removes the edge.

Plan state machine: `Draft →(activate) Active →(review) Pending Review
→(shelve) Shelved →(resume) Active`; `Pending Review →(archive) Archived` and
`Shelved →(freeze) Archived`. `review` has no task gate; `archive` only passes
when the plan's **Work** and **Success Criteria** checklists are all closed.

### Checklists (items)

Every record type has one or more checklists, each bound to a `##` section
(a task's `Acceptance Criteria`, a plan's `Success Criteria`, …). All
checklists of one record share **one numbering space** — `#001`, `#A01`
in a group — so a reference like `PLN-0001#A01` is unambiguous.

```markdown
## Acceptance Criteria
- [x] ✅ `pixi run test` passes
- [ ] deploy script → T-000003 · owner: alex · due: 2026-12-01
```

- States are global and unified: `open` (`[ ]`), `done` (`[x] ✅`),
  `failed` (`[x] ❌`), `dropped` (`[x] 🚫`). Numbers are assigned by the
  tool and never reused (a removed number is a tombstone).
- An item can reference a task (`→ T-000003`, via `--task`); the reference
  renders as a link, and every record whose items reference a task is listed
  in that task's head as `picked by:`.
- A record's checklist is editable in WIP.md only for the one
  `wip: true` checklist; ticking items there is written back by `proj sync`.
- `proj item add|set|check|uncheck|edit|mv|rm T-…#NNN`, `proj item ls`
  (filter `--owner`, `--open`, …).
- Taking an owned item over needs `--reassign` (`--owner <you> --reassign`);
  the hand-over is logged in the record's changelog.
- Gates are per-checklist: entering a gated status (e.g. a task's `Done`)
  needs every item in that checklist closed; for tasks, the whole
  descendant task tree must be terminal too.
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
proj show ID / proj ls [--status S] [--json]
proj docs                  # regenerate docs/CLI.md + docs/MCP.md
proj changelog add|version|release-notes|list
proj hooks install         # pre-commit → proj check
proj bug "…"              # record a proj bug in .proj/bugs.log (never committed)

# per record_type (dynamic; <verb> = one from that type's template `verbs:`)
proj <rt> new TITLE [--supersede N] [--parent N] [-y]
proj <rt> list [--status S]
proj <rt> show ID [--history] [--json]
proj <rt> update ID [-t …] [-s …] [-m "Category: …"]   # title / summary / changelog
proj <rt> <verb> ID        # state-machine step, e.g. decision accept, plan activate

# checklist items (T-…#NNN; group items T-…#A01)
proj item ls|add|set|check|uncheck|edit|mv|rm

# WIP (the "what is in progress" list)
proj wip add|return|ls [--owner <you>]
```

> 完整参考：[`docs/CLI.md`](docs/CLI.md)（`proj docs` 从代码生成，永不漂移）。

## Collaborating over git

Multiple people or agents can share one repo: the committed state is an
append-only **event log** (`.proj/events.jsonl`) — `cache.sqlite` is a
*derived local cache* rebuilt by replay (git-ignored), so there is nothing
binary to merge. Design: `plans/git-collab-design.md`.

**Setup — once per clone:**

```bash
proj hooks install    # git hooks + .gitattributes + a keep-ours merge driver
```

What it installs (and why it works without conflicts):

- **`.gitattributes`** (committed, so every clone gets it): the event log is
  merged with `merge=union` — two people appending events keep **both**
  line-sets, and the append-only log never conflicts. The rendered md files
  (`docs/**/*.md`, `WIP.md`, `CHANGELOG.md`) use a `proj-render` merge driver
  that keeps *ours* on conflict — the md is re-rendered from the merged log
  anyway, so the "lose the other side" conflict is harmless.
- **`pre-commit`** hook → `proj check` (invariants; refuse to push a broken state).
- **`post-merge` / `post-checkout` / `post-rewrite`** hooks → `proj sync --quiet`:
  replay the merged event log into the cache and re-render. After a merge you do
  nothing by hand — the replay is deterministic, so both clones converge.

**What the replay guarantees (P2):**

- **Renumbering, never a lost write.** If two people both claim the next id
  (e.g. both create `T-000003`), replay keeps the first and renumbers the
  later to the next free number — deterministically (a total order), with
  nothing lost. Update your references to the new number (the id stays stable;
  its number follows the replay).
- **Field conflicts survive, deterministically.** Both sides' edits to a field
  are kept; the replay's total order (not file order) decides the winner.
- **Business-rule drift is reported, not auto-fixed.** A merge can leave e.g. a
  task `Done` whose referencing checklist item is still open (replay runs no
  business logic). `proj check` flags it and prints the exact fixing command
  (`proj item set …`); run it to emit a normal event — the two clones must not
  each "auto-fix" and fork the log.

**Daily commands:**

- `proj check` — the log is well-formed, a full replay reproduces the cache,
  ids/numbers are unique, no dangling refs, gate states are consistent.
- `proj compact` — shrink a grown log to one `snapshot` event per record
  (requires a clean tree; it replays and verifies before replacing, then
  reminds you to commit the result and let others sync first).
- `proj info` — event count / log size / last full replay (post-replay).

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
| `proj_bug` / `proj_bug_ls` | `proj bug "…"` (records tool bugs in `.proj/bugs.log`) |

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
- **Multiple people in separate git clones: supported.** The committed source
  of truth is the append-only `.proj/events.jsonl` (merged with `merge=union`),
  and `cache.sqlite` is a derived local cache that any command rebuilds by
  replay — so there is nothing binary to merge and colliding ids are renumbered
  deterministically on replay. Set up a clone with `proj hooks install` and
  merge/checkout/rebase replay automatically; see "Collaborating over git"
  above. Design: `plans/git-collab-design.md`.
- **Network filesystems (NFS/CIFS):** SQLite WAL is unsafe there, so proj
  detects the filesystem and falls back to `journal_mode=DELETE`
  (`proj info` shows the mode and a hint). Avoid writing the same project from
  several machines at once; on NFS the extra cost is mostly in the Python
  startup — keep the pixi/uv environment on local disk (§13 of
  `plans/PLAN.md`).
- **Who made a change:** set `PROJ_AGENT` (e.g. `PROJ_AGENT=claude proj …`, or
  `env` in the MCP server entry above) — every changelog entry records the
  actor; unset falls back to the system username. Also use it as the name you
  claim checklist items with (`--owner <you>`).
- **Tool bugs:** `proj bug "what happened"` (or the MCP `proj_bug` tool)
  appends to `.proj/bugs.log` in the project (never committed, `gitignore`d) —
  auto-recorded with the traceback on unexpected errors too; `proj bug ls`
  lists the reports.

## Tests / lint

```bash
pixi run test        # pytest (temp-dir sandboxes)
pixi run lint        # ruff
pixi run e2e         # full-lifecycle smoke (`.testproj` sandbox, rebuilt each run)
pixi run e2e-full    # e2e + the generated scripts/e2e.sh equivalence check
```
