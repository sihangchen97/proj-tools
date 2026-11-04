# Changelog

All notable changes to this project will be documented in this file.  
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),  
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Performance: cache opens now run the table DDL / legacy migrations only when the
  `meta.schema` stamp is missing or older than `SCHEMA_VERSION` (was re-run on every
  command), and `on_network_fs` results are cached per project path.
- Performance: `proj ls` / `proj-mcp proj_ls` now run ONE query (the parent edge
  rides as a LEFT JOIN) instead of a per-type query plus a `parent_of` per row.
- Performance: the local `ev:prefix_hash` streams over `events.jsonl` in chunks,
  and `ev:last_key` is recomputed over the appended lines only (the stored value
  is folded in); same for a full rebuild whose prefix was untouched.
- Performance: `git_in_progress` caches its verdict per git dir, keyed by a
  microsecond-precision stat signature of the merge/rebase/cherry-pick markers
  plus the index, so a second `Db.begin()` in one command skips the subprocesses.
- The relation lookups (`related_of`, `picked_by`, `tasks_referenced_by`) now
  use one query each with a stable id order.

### Fixed

- A write transaction refused because of git state (a merge / rebase in flight)
  now raises `ProjError`, so the MCP server reports it as a clean
  `error: git: …` instead of a raw `ValueError`.
