# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- SQLite core: `.proj.sqlite` at the project root is the source of truth
  (one table per record_type + a changelog table per type; committed by default).
- YAML templates as schema (`profiles/default/<record_type>.yaml`): record_type,
  dir, numbering, allowed statuses, extra fields, sections (+ hints).
  New record_type = drop one YAML file, zero code changes.
- Layered layout: `core/` (config, db, templates), `engine/` (store, views,
  Record base class), `record_types/` (task, plan, decision, changelog, …).
- md input channel: edit `##` section content → `proj sync` reads it back,
  then re-renders every md from the DB. `proj adopt` absorbs pre-existing md.
- Task relation model: `task.parent` links a task to any record (plan/task/…);
  children resolve at render time (plan Task List sections, main TASKLIST.md).
- First checkbox tick auto-promotes a task `Not Started → In Progress` (on sync).
- `proj task done ID` requires every step (and sub-task) checked.
- Plan state machine: Draft → Active → Pending review → Shelved → Archived,
  with `resume` (Shelved → Active), `archive` and `freeze` (→ Archived).
- Decision supersede as multi-valued bidirectional fields + `proj check`
  cycle/consistency enforcement; `decision toc|graph|link` views.
- `proj check` invariants (md ↔ DB, filename ↔ id, schema ↔ template, nesting)
  and a `proj hooks install` pre-commit hook.
- `--root PATH` global option on every command (run from anywhere, incl. MCP).
- `proj-mcp` stdio MCP server (mcp 2.x): one tool per CLI verb, each with an
  optional `root`; tools return human-readable text, full error messages on failure.
- `proj --version` global flag.
