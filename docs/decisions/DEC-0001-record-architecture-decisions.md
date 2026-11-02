<!-- proj:decision DEC-0001 -->
# DEC-0001. Record architecture decisions

<!-- proj:head:start — TOOL-MANAGED: metadata, summary and changelog are maintained by `proj`; never hand-edit this region (use `proj`) -->
> **status:** `✅ Accepted`  
> **created/done:** 2026-11-02 → 2026-11-02  

_Adopt ADR-style records (SQLite source of truth) for significant decisions._
<details>
<summary>Changelogs</summary>

- `2026-11-02 - ✅ Accepted` · sihang
- `2026-11-02 - ✏️ created` · sihang

✏️ Draft · 🌱 Proposed · ✅ Accepted · 🚫 Rejected · 📛 Deprecated
</details>

<!-- proj:head:end -->
## Context

We need to record the architectural decisions made on this project, with the trade-offs behind them.

## Decision Drivers



## Considered Options



## Decision

We will use Architecture Decision Records, as described by Michael Nygard in this article: https://thinkrelevance.com/blog/2011/11/15/documenting-architecture-decisions. Records are managed by the `proj` tool (https://github.com/sihangchen97/proj-tools), stored as Markdown files under `docs/decisions/` with SQLite as the source of truth (`.proj.sqlite`).

## Consequences

See Michael Nygard's article, linked above. Every significant decision gets a record before production code; views regenerate via `proj update`. Follow-up checks live in the record's Confirmation list (`proj item add DEC-xxxx "<check>"`).

## Confirmation
