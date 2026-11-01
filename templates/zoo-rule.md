# Rule: this project is governed by the `proj` tool

The project root holds a `.proj.sqlite` (the single source of truth) plus
Markdown projections. Never edit the DB by hand and never create/move record
files manually — use the `proj` CLI (or the `proj-*` MCP tools if available).

## How records work

- Every record is a file `docs/<dir>/<ID>-<slug>.md` + a row in the DB.
  The first line is an invisible marker `<!-- proj:<type> <ID> -->`.
- **User-owned (edit freely, then `proj sync`)**: the title line
  (`# <Title>`; a change renames the file to the new slug, the id is stable in
  the filename) and the `##` section bodies.
- **Tool-owned (never hand-edit)**: everything between the
  `<!-- proj:head:start … -->` and `<!-- proj:head:end -->` comments (the
  `> **key:**` metadata block, the italic one-sentence summary) and the
  `<details>` changelog block. Change those via `proj <rt> update|<verb>`.

## Workflow

1. **Decision first** for significant architectural / dependency / API changes:
   `proj decision new "Title" -y`, then advance it with
   `proj decision propose|accept <ID>`. Do not write production code for a big
   change until its decision is `Accepted`.
2. **Track multi-step work as tasks**: `proj task new "Title" -y`. Manage its
   `Acceptance Criteria` checklist with `proj item add <ID> "criterion"` /
   `proj item check <ID>#NN` — tick each one only after local verification
   (tests pass); the first checked item auto-promotes Draft → In Progress.
   Finish with `proj task done <ID>` (requires every descendant task finished
   and every criterion closed).
3. **Group tasks under a plan**: `proj task new … --parent PLN-NNNN`.
   `proj plan review <N>` only passes when all its tasks are Done; move the
   plan with `activate` / `shelve` / `resume` / `archive` / `freeze`.
4. **Record temporary hacks as debt**: `proj debt new "Title" -y` with
   Location, Reason, and Trigger for removal.
5. **Changelog on milestones**: `proj changelog add "Added: …"` (into
   Unreleased); at release: `proj changelog version --bump <level>`.

## Commands you reach for

- `proj <rt> list [--status S]` / `proj <rt> show <ID> [--history]`
- `proj <rt> new "Title" -y [--parent P] [--related R,…]`
- `proj sync` (md → DB → re-render md) · `proj update` (sync + all views)
- `proj check` (invariants; also runs in pre-commit — fix FAILs before pushing)
- `proj render` (DB → md only, when you changed state via the DB/MCP)

## Don't

- Don't hand-edit `TASKLIST.md` / `CHANGELOG.md` (rendered views) or the
  tool-owned head/changelog blocks.
- Don't invent record types or section names — they come from the project's
  `profiles/*.yaml` templates; add a type there if one is genuinely missing.
- Don't bypass the gates (plan review, task done) with direct DB edits.
