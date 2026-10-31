<!-- proj:decision 3f2a1b4c-0000-4000-8000-000000000000 -->
# 0003. Adopt YAML templates

> **status:** 📛 Deprecated  
> **supersedes:** [0001](docs/decisions/0001-record-architecture-decisions.md)  
> **superseded_by:** [0004](docs/decisions/0004-new-platform.md)  
> **started:** —  
> **done:** 2026-10-29

Summary: templates become the schema

| Date | Category | Change |
|---|---|---|
| 2026-10-29 | Changed | status Proposed → Accepted |
| 2026-10-29 | Added | created |

## Context

We want the record schema to live in a plain, user-editable file.

## Decision

Use one YAML template per record_type.

## Consequences

A new record_type is one YAML file, zero code changes.
