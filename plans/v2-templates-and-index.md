# v2 Design — Templates as the unit of function, JSON index, frontmatter contract

## 0. Decision summary

- **No database.** Source of truth = the markdown files. `.proj/index.json`
  is a *derived* index, rebuilt any time by `proj update` (zero drift risk).
  "One table per kind" becomes one section per kind inside the JSON index:
  `records.<kind> = {uuid: {…frontmatter fields… + sections}}` — same shape
  as an SQL table (uuid = primary key), but diffable and rebuildable.
- **Templates replace tiers.** Template folders under `templates/` are the
  unit of function. `templates/default/` is always the base; every other
  folder is an **additive profile**. `proj init --profile cv` copies
  `default/` + `cv/`. Renaming the old `--tier` flags to `--profile`.
- **One generic engine.** Every artifact kind (adr/plan/task/debt/incident/
  experiment/…) runs the SAME code path: template render → frontmatter
  contract → validate → index. Kind-specific behavior (allowed status
  values, numbering style) is declared *in the template*, not in code.

## 1. Template = profile = function

```
templates/                      # project root, committed, user-editable
  default/
    adr.md
    plan.md
    debt.md
    incident.md
    experiment.md
  cv/                           # additive profile
    dataset.md
    benchmark.md
  infra/
    spec.md
    dev-guide.md
  quality/
    contributing.md             # non-record scaffolds (no uuid) may live here too
```

Each record-template starts with a **template header** (consumed by proj,
stripped from output), then the document body:

```markdown
<!-- proj-template
kind: adr
dir: docs/adr
numbered: 4            # 4 = NNNN prefix; "date" = YYYY-MM-DD; 0 = none
status-allowed: Proposed, Accepted, Deprecated, Superseded by ADR-{id}
-->
---
id: "{{id}}"
uuid: "{{uuid}}"
title: "{{title}}"
status: Proposed
created: "{{date}}"
supersedes: "{{supersedes}}"
---

# {{id-int}}. {{title}}

## Context
...
## Decision
...
## Consequences
...
```

Placeholders: `{{uuid}} {{id}} {{id-int}} {{title}} {{slug}} {{date}}
{{supersedes}}`. `init` copies package-bundled templates → `templates/`
(only files that don't exist — user edits win, same idempotence rule).

The `kind` key is the "table name". Adding a new artifact type =
drop a new template file, zero code changes (list/new/submit/update/check
all read the kind list from the template headers).

## 2. Frontmatter contract (per record file)

| Field | Rule | Validated on `submit` |
|---|---|---|
| `id` | must equal filename prefix (`NNNN` or date) | ✅ + uniqueness within kind |
| `uuid` | uuid4, generated once at `new`, **immutable** | ✅ format, global uniqueness, unchanged vs index |
| `title` | non-empty | ✅ |
| `status` | must be in template's `status-allowed` | ✅ |
| `created` | ISO date, set at `new`, immutable | ✅ unchanged |
| kind-specific (e.g. `supersedes`) | optional | presence/format |

Body headings (`## …`) are extracted into `sections` in the index for
lookup/rendering; heading names are free-form (validated only that a
recognized set exists where the template declares it — kept permissive).

## 3. `.proj/index.json`

```json
{
  "version": 1,
  "records": {
    "adr": {
      "3f2a…": {
        "id": "0001", "title": "…", "status": "Accepted",
        "created": "2026-10-28", "path": "docs/adr/0001-….md",
        "supersedes": [], "sections": {"context": "…", "decision": "…"}
      }
    },
    "plan": { "…": { … } }
  }
}
```

- Written by `proj new` (insert), `proj submit` (upsert), `proj update`
  (full rebuild).
- **Orphan detection**: file on disk but not in index → flagged by
  `update`/`check`; index entry with no file → flagged as dangling.
- Human-glanceable: sorted by kind then id (not pure dict order).

## 4. Unified command surface

Generic verbs (work for every kind):

```
proj new <kind> TITLE [-s SUPERSEDED] [-y]   # render template, generate uuid, write + index
proj list <kind> [--status S]                # from index
proj show <kind> ID                          # print record from index (title/status/sections)
proj status <kind> ID <STATUS>               # write frontmatter status (validates allowed set)
proj submit [FILE…]                          # no args = all managed dirs; validate + upsert index
proj update                                  # full rescan + rebuild index; report orphans/dangling/drift
proj check                                   # adds: every managed file has valid frontmatter, is in index, uuids stable
```

Kind-specific verbs become THIN wrappers or disappear:

| v1 command | v2 |
|---|---|
| `proj debt repay N` / `experiment close N` / `adr status` | `proj status debt N "Repaid"` (status-allowed per template) |
| `proj decision link/toc/graph` | keep (ADR-specific); `graph`/`toc` render FROM THE INDEX |
| `proj plan shelve/resume` | keep (moves files between kinds; updates index both sides) |
| `proj task done` | keep (status→complete + move to plans, index both sides) |
| `proj changelog …` | unchanged (CHANGELOG.md is one document, not records) |
| `proj init --tier X` | `proj init --profile X` (copies template folders) |
| `proj hooks install` | unchanged |

Flow the tool enforces:

```
proj new → md with fresh uuid  ──┐
user/LLM edits md (git-auditable) │  proj submit/update → validate → index.json
                                 └─ proj list/show/toc/graph/check ← read the index
```

Rendering direction is one-way per artifact: template→md at creation;
md→index afterwards. proj never rewrites user content (except the
frontmatter fields it owns: id/uuid/status/created).

## 4c. Module map — what is generic vs individually maintained

### A. Generic record engine — ONE code path, template-driven

Every kind below runs the same pipeline:
`template → render (uuid/id/created/history) → md file → frontmatter
contract → validate (submit) → index.json → list/show/status/graph`.

| kind | dir | numbering | status-allowed (template-declared) | notes |
|---|---|---|---|---|
| `adr` | `docs/adr/` | `NNNN` | Proposed, Accepted, Deprecated, Superseded by ADR-{id}, Supersedes ADR-{id} | `supersedes` field; toc/graph/link are ADR-specific *renderers* over the index |
| `task` | `tasks/` | `NNNN` | Active, Blocked, Done | **one file per task** (see decision D1); TASKLIST.md becomes a rendered view |
| `plan` | `docs/plans/` | `NNNN` | Draft, Shelved, Archived | shelve/resume = engine `move` ops that keep id + log `moved` history |
| `debt` | `docs/tech-debt/` | `NNNN` | Proposed, In progress, Repaid | `repay` = `proj status debt N "Repaid"` |
| `incident` | `docs/incidents/` | `YYYY-MM-DD` | Open, Closed | date numbering |
| `experiment` | `docs/experiments/` | `NNNN` | Running, Closed | `commit` field |

**Adding a new kind = dropping one template file** (`templates/default/foo.md`
with a `<!-- proj-template kind: foo … -->` header). Zero code changes: the
kind registry is built by scanning template headers at runtime.

Engine modules (shared by ALL kinds):
- `frontmatter.py` — split/join `---` blocks, `yaml.safe_load/dump`
- `templates.py` — scan `templates/*/` headers → kind registry; render with
  `{{uuid}} {{id}} {{id-int}} {{title}} {{slug}} {{date}}` placeholders
- `records.py` — `Record` class: frontmatter fields + `sections` (heading
  split) + `history` + `add_event()`; read/write one md file
- `index.py` — `.proj/index.json` build/rebuild, orphan & dangling detection
- `validate.py` — the submit contract (uuid format/uniqueness/immutability,
  id==filename, status ∈ allowed, history entry shape)

### B. Individually maintained modules (NOT record-shaped)

| module | why it can't use the record engine |
|---|---|
| `changelog.py` | CHANGELOG.md is ONE document with version sections, not N records. No uuid, no per-entry identity. Keeps its own parser: `version --bump`, `add`, `release-notes` |
| `scaffold.py` | Static scaffolds: CONTRIBUTING.md, .env.example, .github/*, DATASET.md, BENCHMARK.md, docs/specs/, docs/dev/. No frontmatter, no uuid — copied once at init, user owns them after |
| `adr.py` (thin) | ADR-specific *renderers*: `toc`, `graph` (mermaid), `link` (cross-refs). All read FROM THE INDEX; no own storage |
| `taskview.py` (replaces tasklist.py) | Renders TASKLIST.md from the `task` kind in the index (progress table). Plus `shelve`/`resume`/`done` as thin wrappers over engine move/status ops with task-specific rules (done requires all steps checked) |
| `check.py` | Infrastructure: invariants across kinds + frontmatter/index consistency |
| `config.py`, `hooks.py`, `cli.py` | Infrastructure: root discovery, git hooks, command wiring |

### C. Rendered views (generated — never hand-edited)

| view | source | regenerated by |
|---|---|---|
| `TASKLIST.md` (root) | `task` kind records in `tasks/` | `proj update` / `proj task render` |
| `.proj/toc/adr.md` | `adr` index | `proj decision toc` |
| `.proj/index.json` | all record files | `proj update` / `submit` / `new` |
| mermaid graph (stdout/file) | `adr` index | `proj decision graph` |

Rule: anything in C is overwritten freely by proj; anything in A is
user/LLM-owned (proj only touches its own frontmatter fields + appends
history).

### D. Decisions to confirm

- **D1 (task storage)**: tasks become one-file-per-record in `tasks/`
  (e.g. `tasks/0001-ship-v1.md`), and TASKLIST.md is a *rendered view*.
  Pros: task/plan share the exact same engine; shelve/resume = file move +
  history event; no block-parsing of a shared file. Cons: TASKLIST.md is no
  longer hand-editable (edit the task file or use `proj task add`).
  Alternative: keep TASKLIST.md as the source with `## blocks` (v1 behavior)
  and treat `task` as the one engine exception.
- **D2 (task dir)**: `tasks/` at repo root vs `docs/tasks/`. Root keeps
  "work in progress" visible next to TASKLIST.md; docs/ keeps all
  documentation together.

### E. Command map after v2

```
proj new <kind> TITLE [-s N] [-y]      # engine
proj list <kind> [--status S]          # engine (from index)
proj show <kind> ID [--history]        # engine (from index)
proj status <kind> ID STATUS           # engine (validates allowed set)
proj submit [FILE...]                  # engine (validate + upsert index)
proj update                            # engine (rebuild index + render views)
proj check                             # engine + invariants
proj decision toc|graph|link           # ADR renderers over index
proj task add|done|list                # thin wrappers (task kind)
proj plan new|shelve|resume|list       # thin wrappers (plan kind)
proj changelog version|add|release-notes  # standalone module
proj init --profile X                  # scaffold + copy templates
proj hooks install                     # unchanged
```

`debt repay` / `experiment close` / `incident close` retire in favor of
`proj status <kind> ID <STATUS>` (status-allowed comes from each template).

## 4b. Per-record changelog (audit trail)

Every record carries its own history — stored in **frontmatter** (source of
truth; the index is derived and rebuildable, so history must live in the file):

```yaml
---
id: "0002"
uuid: 3f2a…
title: Use pixi
status: Accepted
created: 2026-10-28
history:
  - {date: 2026-10-28, event: created}
  - {date: 2026-10-29, event: status, from: Proposed, to: Accepted}
  - {date: 2026-10-30, event: moved, from: task, to: plan}   # shelve/resume keep id
---
```

- Generic `Record` class (in `records.py`) owns `history: list[HistoryEvent]`;
  every mutating command (`new`, `status`, `shelve`/`resume`, `repay`…) calls
  `rec.add_event("status", from=…, to=…)` before writing.
- `shelve/resume` keep the original id (confirmed) and log a `moved` event.
- History is **not rendered** in the md body (stays in frontmatter);
  `proj show <kind> <id> --history` prints it.
- `submit` validates entry shape (date + event present) but never rewrites
  user-added entries; proj only appends.

## 5. Implementation plan (ordered)

1. `frontmatter.py` — parse/serialize YAML frontmatter + body split (dep: pyyaml)
2. package-bundled `templates/` (default + cv/infra/quality) with template headers
3. `index.py` — Index dataclass, load/save `.proj/index.json`, extract sections
4. generic `records.py` rewrite — `Record` class (frontmatter incl. `history`
   + `add_event`), kind registry from template headers; `new_kind`
5. `submit` + `update` commands + uuid/id/status validation rules
6. `check` extension; `list/show/status` from index
7. `init` copies templates; `--tier` → `--profile`
8. retire hardcoded per-kind templates (ADR_TEMPLATE etc.) — read from files
9. tests: frontmatter round-trip, uuid immutability, submit validation,
   index rebuild determinism, orphan/dangling, full suite + e2e

## 6. Open risks

- pyyaml is the only new runtime dep (fine for uvx).
- Templates with `<!-- proj-template -->` header: users editing templates must
  keep it; `check` should validate template headers (nice-to-have).
- `shelve/resume` change a record's kind+dir — index keys by uuid so identity
  survives; id renumbering on resume is a design point (keep same id, new dir).
