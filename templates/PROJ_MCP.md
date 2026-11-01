# proj MCP 参考

> 入口：`proj-mcp`（stdio MCP server，`mcp` 2.x `MCPServer`）。
> 每个 tool 对应一个 CLI 动词，参数同名（`--x` → `x`），并多一个 `root`：
> 绝对项目路径，= CLI 的 `--root`。优先级：tool `root` > `$PROJ_HOME` > 当前目录。
> 从项目目录启动 client（如 Claude Code 在仓库里）不需要任何配置；从全局配置启动
> （client cwd ≠ 项目）就给每个 tool 传 `root`，或设 `$PROJ_HOME`。
> 校验失败不抛错，而是返回 `error: …` 文本（agent 可读）。

## Tools

### `proj_init`

Scaffold a new project: files, DB tables (lock wins over templates), seed, adopt.
root: absolute project path (default: the current directory).
profile: default plus additive names (cv, infra, quality, ...); the choice
is pinned in .proj.lock — repeat inits append new profiles.

### `proj_new`

Create a record from its template (DB insert + rendered md).
record_type: registered type (decision/task/plan/debt/incident/experiment/…).
supersede: ids being replaced, same type, comma-separated.
related: related record ids, any type, comma-separated (unordered).
parent: parent record id (task only). commit: git commit hash.
summary: one-sentence summary.

### `proj_sync`

md → DB (validate, normalize) → re-render all md. files: comma-separated
paths (default: all managed dirs). FAIL lines mean the DB was not fully updated.

### `proj_check`

Validate DB ↔ lock ↔ templates ↔ md invariants. 'OK' or FAIL lines.

### `proj_list`

List records of a record_type (from DB), optionally filtered by status.

### `proj_show`

Show one record (from DB); history: append its full changelog.

### `proj_verb`

Run a template verb (a state-machine step) on a record, e.g.
`('decision','accept','DEC-0002')`, `('plan','activate','PLN-0001')`,
`('task','done','T-000001')`. verb must be one the record_type's template
declares; title/summary/message optionally also update it after the step.

### `proj_update_record`

Update a record's title / summary (a title change renames its md file);
message: optionally append a changelog entry ('Category: text').

### `proj_update`

sync all + re-render every view (TASKLIST.md, CHANGELOG.md, view sections).

### `proj_info`

What this project manages (record_types, counts, statuses) + verb cheatsheet.

### `proj_changelog_add`

Add a CHANGELOG.md entry: 'Category: text' (Added/Changed/Fixed/…; plain
text ok). version: default the Unreleased placeholder.

### `proj_task_move`

Move an existing task under a (possibly new) parent record, or detach it.
parent: new parent id (any record type); detach: True to unparent. Exactly
one of the two. Refused for itself / its own descendants / closed records.

### `proj_item_ls`

List items as 'ID#A01  state  text  [owner]  [due]'. ref: one record id
(omit = every record); group / open_only / record_type / owner / overdue
filter.

### `proj_item_add`

Add an item to a record's checklist (auto-numbered, appended to its
group). group: the group letter L (default the ungrouped list); after:
insert right after ITEM; state/note/owner/due optional. Returns the fresh
item list.

### `proj_item_set`

Set an item's state (item = 'ID#n'). note / commit optional. Returns the
fresh item list.

### `proj_item_check`

Mark an item ('ID#n') closed (first closed state). Returns the fresh item
list.

### `proj_item_uncheck`

Re-open an item ('ID#n') (first unclosed state). Returns the fresh item
list.

### `proj_item_edit`

Edit an item's ('ID#n') text / note / owner / due (empty string clears).
Returns the fresh item list.

### `proj_item_mv`

Move an item ('ID#A01'): exactly one of group (into L, fresh number) /
ungroup (to the ungrouped list, fresh number) / after (right after ITEM) /
first (top of its group). Cross-group moves get a fresh number. Returns the
fresh item list.

### `proj_item_rm`

Remove an item ('ID#A01'); its number is never reused. Returns the fresh
item list.

### `proj_item_group_add`

Add a group; the tool assigns the next free letter (A-Z, never reused).
Returns the fresh item list.

### `proj_item_group_rename`

Rename a group. Returns the fresh item list.

### `proj_item_group_rm`

Delete a group; `items` = 'ungroup' or 'delete' when it holds items.

### `proj_item_group_mv`

Move a group: exactly one of after / first. Returns the fresh item list.

