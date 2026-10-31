---
<!-- proj:decision 6b374ee5-d6da-4356-bdb0-02608ebf4890 -->
status: 🌱 Proposed
---
# 1. Record architecture decisions

Summary: —

| Date | Category | Change |
|---|---|---|
| 2026-10-29 | Added | adopted into proj |

## Context

We want a lightweight, reviewable record of non-trivial architectural decisions and the trade-offs behind them.

## Decision

We use decision records managed by the `proj` tool, stored as Markdown files under `docs/decisions/` with SQLite as the source of truth (`.proj.sqlite`).

## Consequences

Every significant decision gets a record before production code. Views regenerate via `proj update`.
