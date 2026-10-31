# Contributing

## Workflow rules

1. **Decision first**: before significant architectural, dependency or API
   changes, create a decision (`proj new decision "Title"`) and get it
   Accepted. Do not write production code for big changes until it is accepted.
2. **Task tracking**: multi-step work lives as task records
   (`proj new task "Title"`). Mark steps `[x]` only after local verification
   (tests/lint). Finish with `proj task done <ID>`.
3. **Plans**: attach tasks to a plan with `--parent N`; promote/shelve them by
   moving the plan (`proj plan activate|shelve N`). Sub-tasks attach to a task
   the same way; a task is only done when its steps and sub-tasks are done.
4. **Changelog**: on milestones add an entry: `proj changelog add "Added: …"`.
5. **Tech debt**: temporary hacks get their own record
   (`proj new debt "Title"`) with Location, Reason, and Trigger for removal.
6. **Secrets**: never hardcode secrets, private IPs or tokens. Read from the
   environment; commit only `.env.example`.
7. **Commits**: one commit per feature or bugfix. No unrequested reformatting.
8. **Check**: run `proj check` (also runs in pre-commit) before pushing.
9. **MD files**: edit `##` section content only, then `proj sync`.
   Never edit the marker line, `Status:`/field lines or the changelog table.
