# proj MCP 参考

> 入口：`proj-mcp`（stdio MCP server，`mcp` 2.x `MCPServer`）。
> 每个 tool 对应一个 CLI 动词，参数同名（`--x` → `x`），并多一个 `root`：
> 绝对项目路径，= CLI 的 `--root`。优先级：tool `root` > `$PROJ_HOME` > 当前目录。
> 从项目目录启动 client（如 Claude Code 在仓库里）不需要任何配置；从全局配置启动
> （client cwd ≠ 项目）就给每个 tool 传 `root`，或设 `$PROJ_HOME`。
> 校验失败不抛错，而是返回 `error: …` 文本（agent 可读）。

## Tools

### `proj_init`

Scaffold a new project: files, DB tables (lock wins over templates), seed,
adopt. root: absolute project path (default: the current directory).
profile: default plus additive names (cv, infra, quality, …); the choice is
pinned in .proj.lock — repeat inits append new profiles.

### `proj_new`

Create a record from its template (DB insert + rendered md).
record_type: registered type (decision/task/plan/debt/incident/experiment/…).
supersede: ids being replaced, same type, comma-separated.
related: related record ids, any type, comma-separated (unordered).
parent: parent record id (task only). commit: git commit hash.
summary: one-sentence summary.

### `proj_sync`

md → DB (validate, normalize) → re-render all md. files: comma-separated
paths (default: all managed dirs). FAIL lines mean the DB was not fully
updated.

### `proj_adopt`

Absorb pre-existing md files into the DB (id marker if present, else an
id check). files: comma-separated paths (default: all managed dirs).

### `proj_render`

Regenerate md from the DB (no md input). ref: comma-separated record ids
and/or record_types (default: every record + every view). Unsynced hand
edits are ingested first (problems abort before any file is written).

### `proj_check`

Validate DB ↔ lock ↔ templates ↔ md invariants. 'OK' or FAIL lines.

### `proj_ls`

List records (from DB), one line each (id, status, title). record_type:
restrict to one type (name or code); omitted = every record_type.
status: filter (display name, e.g. 'In Progress').

### `proj_show`

Show one record (from DB); the id carries its type so record_type is
optional. history: append the full changelog. as_json: a JSON object
(id/type/title/status/created/summary/parent/changelog) instead of text.

### `proj_update`

Update a record's title / summary (a title change renames its md file);
message: optionally append a changelog entry ('Category: text'). The id
carries the type — record_type is optional.

### `proj_verb`

Run a template verb (a state-machine step) on a record, e.g.
`('DEC-0002','accept')`, `('PLN-0001','activate')`, `('T-000001','done')`.
The id carries the type — record_type is optional. verb must be one the
type's template declares; `from: Any` verbs work from any status.
title/summary/message: optionally also update them after the step
(message appends a changelog entry, 'Category: text').

### `proj_info`

What this project manages (record_types, counts, statuses) + the verb
cheatsheet (which verb moves which record where, with its gate).

### `proj_changelog_add`

Add a CHANGELOG.md entry: 'Category: text' (Added/Changed/Fixed/…; plain
text ok). version: default the Unreleased placeholder.

### `proj_changelog_version`

Release a version: move Unreleased into a version (name: the version,
e.g. v0.1.0; bump: major | minor | patch from the latest). Exactly one.

### `proj_changelog_release_notes`

Print one version's CHANGELOG section (for PR/tag bodies).

### `proj_changelog_list`

List released versions (newest first): 'vX.Y.Z  date'.

### `proj_task_move`

Re-home an existing task: parent = the new parent record id (any type),
detach = True to remove the parent edge entirely (the task becomes
Unparented). Exactly one of parent/detach. Refused when the target is the
task itself, one of its own descendants (cycle), or a closed record.

### `proj_item_ls`

List items as 'ID#A01  state  text  [owner]  [due]'. ref: one record id
(omit = every record); group / open_only / record_type / owner / overdue
filter. as_json: a JSON array of objects (ref, record, no, group, state,
closed, text, note, owner, due).

### `proj_item_add`

Add an item to a record's checklist (auto-numbered, appended to its
group), or a whole new group. new_group=true: `text` is the GROUP name —
the next free letter (A-Z, never reused) is assigned. group: the group
letter L (default the ungrouped list); after: insert right after ITEM;
state/note/owner/due optional. Returns the fresh item list.

### `proj_item_update`

Change an item or a group of a record's checklist (item = 'ID#nn'; a
group = 'ID#L00', its X00 row — `text` on a group renames it). Pass only
the fields to change. state: one of the checklist's states, or 'closed'
(the first closed state) / 'open' (the first unclosed state); text/note/
owner/due on an item (an empty string clears a field); commit: a git
commit hash. Move: exactly one of group (into L, fresh number) / ungroup
(to the ungrouped list) / after (right after ITEM) / first (top of its
group) — for a group, after/first moves the group. Returns the fresh item
list. Refused when it would un-close an item on a record in a closed
(gates) status.

### `proj_item_rm`

Remove an item ('ID#nn'; its number is never reused) or a whole group
('ID#L00' — when the group still holds items, `items` must be 'ungroup' or
'delete'). Returns the fresh item list.

