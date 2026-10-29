# proj

Project governance tool: ADRs, tasklists, changelogs, tech debt, incidents,
experiments — one CLI. User docs live in `docs/` and `TASKLIST.md` /
`CHANGELOG.md` at root; tool-owned generated files live in `.proj/`.

## Install

```bash
uv tool install proj        # or: uvx proj
```

## Layout produced by `proj init`

```
TASKLIST.md                    # active work only
CHANGELOG.md                   # Keep a Changelog format
CONTRIBUTING.md
DATASET.md                     # tier: cv
BENCHMARK.md                   # tier: cv
.env.example                   # tier: quality
docs/adr/NNNN-slug.md          # ADRs (heading "# N. Title" un-padded)
docs/plans/YYYY-MM-slug.md     # plans: drafts, shelved, or archived (frozen)
docs/tech-debt/NNNN-slug.md    # tier: quality
docs/incidents/YYYY-MM-DD-slug.md   # tier: quality
docs/experiments/NNNN-slug.md  # tier: cv
docs/specs/  docs/dev/         # tier: infra
.github/ISSUE_TEMPLATE/{bug,feature}.md
.github/PULL_REQUEST_TEMPLATE.md
.proj/toc/adr.md               # generated TOC (tool-owned)
.proj/githooks/pre-commit      # installed by `proj hooks install`
```

Project root is found by walking up from the cwd looking for
`pyproject.toml`, `.git`, or `.proj/`. Optional overrides in
`pyproject.toml`:

```toml
[tool.proj]
tiers = ["core", "quality"]
```

## Command reference

Global options: `proj --help`, `proj --install-completion` (bash/zsh/fish),
`proj --show-completion`. Any subcommand accepts `--help`.

### `proj init` — scaffold (idempotent, never overwrites)

```
proj init [--tier TIER]...
```

| Option | Meaning |
|---|---|
| `--tier TIER` | Repeatable. One of `core` (default), `cv`, `infra`, `quality`. |

- `core`: `docs/adr/`, `docs/plans/`, `CHANGELOG.md`, `TASKLIST.md`, ADR 0001,
  `CONTRIBUTING.md`, `.github/` templates, `.proj/`
- `cv`: `docs/experiments/`, `DATASET.md`, `BENCHMARK.md`
- `infra`: `docs/specs/`, `docs/dev/`
- `quality`: `docs/tech-debt/`, `docs/incidents/`, `.env.example`

### `proj check` — validate invariants (CI / pre-commit), exit 1 on failure

No options. Checks:
- ADR heading number matches filename number
- Every ADR has a recognizable status (`Proposed`, `Accepted`, `Deprecated`,
  `Superseded by ADR-NNNN`, `Supersedes ADR-NNNN`, `Repaid…`, `Closed…`)
- `TASKLIST.md` contains no fully-completed task (must be archived)
- `CHANGELOG.md` versions are newest-first, `[Unreleased]` first if present

### `proj decision` — Architecture Decision Records (`docs/adr/`)

```
proj decision new TITLE [-s SUPERSEDED] [-y]
```

| Option | Meaning |
|---|---|
| `-s, --supersede N` | New ADR supersedes ADR N (old one gets `Superseded by ADR-<new>`) |
| `-y, --yes` | Don't open `$VISUAL`/`$EDITOR` after creation |

```
proj decision list [--status STATUS]
```

| Option | Meaning |
|---|---|
| `--status STATUS` | Filter by exact status, e.g. `Accepted`, `Deprecated` |

```
proj decision link TARGET NNNN [NNNN...]
```

Add/extend a `See also: ADR-N, …` list on ADR TARGET. Idempotent.

```
proj decision toc
```

Regenerate `.proj/toc/adr.md`.

```
proj decision graph [OUT_FILE]
```

Emit a mermaid graph of supersede edges. `OUT_FILE` optional (default stdout).

### `proj task` — work in progress RIGHT NOW (`TASKLIST.md`)

```
proj task add TITLE
```

Append an active task block to `TASKLIST.md` (error if the title exists).

```
proj task done TITLE
```

Requires every sub-step checked `[x]`; freezes the record into
`docs/plans/YYYY-MM-slug.md` and removes it from `TASKLIST.md`.

```
proj task list
```

List active tasks with `done/total` progress.

### `proj plan` — overall plans, can be shelved (`docs/plans/`)

`docs/plans/` holds drafts, paused work, and finished archives.

```
proj plan new TITLE
```

Create a draft plan `docs/plans/YYYY-MM-slug.md` from the plan template.

```
proj plan shelve TITLE
```

Park an in-progress (incomplete) task from `TASKLIST.md` into `docs/plans/`
marked `Shelved <date> (n/m done)`. The checklist state is preserved.

```
proj plan resume TITLE
```

Move a plan back into `TASKLIST.md` as an active task (checklist state
preserved). Refuses if a task with the same title is already active.

```
proj plan list
```

List plans in `docs/plans/`, newest first, with their status note
(Archived / Shelved / Plan created).

### `proj changelog` — CHANGELOG.md

```
proj changelog version [--bump major|minor|patch] [--explicit VERSION]
```

Create a new released version section (dated) plus a fresh `[Unreleased]`
above it. `--bump` computes the next semver from the current released version
(falls back to `[project] version` in pyproject.toml); `--explicit` overrides.
Give exactly one of the two.

```
proj changelog add "<SECTION> <text>" [--version VERSION]
```

`<SECTION>` ∈ `Added`, `Changed`, `Fixed`, `Deprecated`, `Removed`, `Security`.
`--version` targets a specific version section (default `Unreleased`; the
section is created if missing).

```
proj changelog release-notes VERSION
```

Print that version's section as standalone markdown (for PR/tag bodies).

### `proj debt` — tech debt (`docs/tech-debt/`)

```
proj debt new TITLE [-y]        # -y = skip editor
proj debt list
proj debt repay NNNN            # status -> "Repaid on YYYY-MM-DD"
```

### `proj incident` — post-mortems (`docs/incidents/`, date-prefixed)

```
proj incident new TITLE [-y]
proj incident list
```

Every incident template includes a **Prevention rule** section (actionable
rule to add to AI guidelines so the failure can't recur).

### `proj experiment` — experiment records (`docs/experiments/`)

```
proj experiment new TITLE [-c COMMIT] [-y]
proj experiment list
proj experiment close NNNN      # status -> "Closed on YYYY-MM-DD"
```

### `proj hooks` — git hooks

```
proj hooks install
```

Writes `.proj/githooks/pre-commit` (runs `proj check`) and sets
`git config core.hooksPath .proj/githooks`. The hook is committed and shared
with the repo.

## Quick start

```bash
proj init --tier core --tier quality
proj decision new "Use pixi for dev envs"
proj task add "Ship v1"
proj plan new "Roadmap to v1"
proj changelog add "Added new feature"
proj check
```

## Development (this repo)

Uses pixi for local dev/test (declared in `pyproject.toml`):

```bash
pixi run test        # pytest
pixi run lint        # ruff
```

Design: `plans/architecture.md`.
