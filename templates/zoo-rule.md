# Rule: this project is governed by the `proj` tool

The project root holds a `.proj.sqlite` (the single source of truth) plus
Markdown projections. Never edit the DB by hand and never create/move record
files manually — use the `proj` CLI (or the `proj_*` MCP tools if available).

## How records work

- Every record is a file `docs/<dir>/<ID>-<slug>.md` + a row in the DB.
  The first line is an invisible marker `<!-- proj:<type> <ID> -->`.
- **User-owned (edit freely)**: the title line (`# <ID>. <Title>`; a change
  renames the file to the new slug, the id is stable) and the `##` section
  bodies, including checklist lines. Your edits are picked up automatically by
  the next `proj` write command (or run `proj sync`).
- **Tool-owned (never hand-edit)**: everything between the
  `<!-- proj:head:start … -->` and `<!-- proj:head:end -->` comments (the
  `> **key:**` metadata block, the italic one-sentence summary) and the
  `<details>` changelog block. Change those via `proj <type> update|<verb>`.
- **Checklist items** are numbered by the tool: `#001` (ungrouped) or `#A01`
  (group A, shown under a `### A · <name>` heading). Refer to one as
  `<ID>#A01`. A line can carry ` — note`, ` · owner: <name>`, ` · due: YYYY-MM-DD`.

## Workflow

1. **Decision first** for significant architectural / dependency / API changes:
   `proj decision new "Title" -y`, then advance it with
   `proj decision propose|accept <ID>`. Do not write production code for a big
   change until its decision is `Accepted`.
2. **Track multi-step work as tasks**: `proj task new "Title" -y -s "one line"`.
   Manage its `Acceptance Criteria` checklist with `proj item add <ID> "criterion"` /
   `proj item check <ID>#001` — tick each one only after local verification
   (tests pass); the first checked item auto-promotes Draft → In Progress.
   Finish with `proj task done <ID>` (requires every sub-task finished and every
   criterion closed).
3. **Group tasks under a plan**: `proj task new … --parent PLN-NNNN`.
   `proj plan review <ID>` only passes when all its tasks are finished; move the
   plan with `activate` / `shelve` / `resume` / `archive` / `freeze`.
4. **Record temporary hacks as debt**: `proj debt new "Title" -y` with
   Location, Reason, and Trigger for removal.
5. **Changelog on milestones**: `proj changelog add "Added: …"` (into
   Unreleased); at release: `proj changelog version --bump <level>`.

## Working with others (several people or agents)

- **Work is assigned per checklist item, by `owner`** (records themselves have
  no assignee). Claim an item before starting it:
  `proj item add <ID> "what you will do" --owner <you>` for new work, or
  `proj item edit <ID>#A01 --owner <you>` for an existing item.
  Use the same name every time (your agent name, e.g. `alex`).
- **Don't take an item someone else owns**; ask, or add your own item.
- **Your open work**: `proj item ls --owner <you> --open`.
- **What is in progress**: `WIP.md` / `proj wip ls`. Records join WIP
  automatically when they start (e.g. a task going In Progress) and leave when
  they finish; add or remove one by hand with `proj wip add|return <ID>`.
  Items in WIP.md show their owner and can be ticked there too.
- **When you finish an item**, close it (`proj item check <ID>#A01`, or
  `proj item set <ID>#A01 <state> --note "<evidence>"`) — a note with the
  evidence (PR, commit, numbers) helps the next person.

## Commands you reach for

- `proj ls [--type T] [--status S]` / `proj show <ID> [--history]` (any type)
- `proj <type> new "Title" -y [-s "summary"] [--parent P] [--related R,…]`
- `proj item ls|add|check|set|edit|mv|rm …` (checklists; `proj item --help`)
- `proj wip ls|add|return …`
- `proj sync` (md → DB → re-render everything)
- `proj check` (invariants; also runs in pre-commit — fix FAILs before pushing)
- Add `--json` to `ls` / `show` / `item ls` for machine-readable output.

## Don't

- Don't hand-edit `WIP.md`'s tool-owned parts / `CHANGELOG.md` (rendered views)
  or the tool-owned head/changelog blocks of a record.
- Don't invent record types or section names — they come from the project's
  `profiles/*.yaml` templates; add a type there if one is genuinely missing.
- Don't bypass the gates (plan review, task done) with direct DB edits.
