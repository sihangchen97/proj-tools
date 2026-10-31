#!/usr/bin/env bash
# e2e smoke test: full proj CLI lifecycle in the .testproj sandbox.
# Usage: pixi run e2e   (or: bash scripts/e2e.sh)
set -u
cd "$(dirname "$0")/.."

SANDBOX=.testproj
# prefer the pixi dev env's proj (a system ~/.local/bin/proj may be stale);
# resolve to an absolute path — the script cd's into the sandbox later
if [ -x .pixi/envs/dev/bin/proj ]; then PROJ="$(pwd)/.pixi/envs/dev/bin/proj"; else PROJ=proj; fi

PASS=0; FAILED=0
ok()   { PASS=$((PASS + 1)); echo "ok   $1"; }
fail() { FAILED=$((FAILED + 1)); echo "FAIL $1" >&2; }

expect_ok() { # <desc> <fixed-string> -- cmd...
  local desc=$1 pat=$2; shift 2; [ "${1:-}" = -- ] && shift
  local out
  if out=$("$@" 2>&1) && grep -qF -- "$pat" <<<"$out"; then ok "$desc"
  else fail "$desc (missing: $pat)"; printf '%s\n' "$out" | sed 's/^/     /'; fi
}

expect_fail() { # <desc> -- cmd...
  local desc=$1; shift; [ "${1:-}" = -- ] && shift
  local out
  if out=$("$@" 2>&1); then fail "$desc (unexpectedly succeeded)"
  else ok "$desc (rejected)"; fi
}

# ---- sandbox -------------------------------------------------------------------
rm -rf "$SANDBOX"
mkdir -p "$SANDBOX"
git -C "$SANDBOX" init -q
cd "$SANDBOX"

# 1. init + seed
expect_ok "init scaffolds"             "Created:" -- $PROJ init
[ -f .proj.sqlite ] && ok "db at .proj.sqlite" || fail "db file missing"
[ -f docs/decisions/DEC-0001-record-architecture-decisions.md ] \
  && ok "seed decision 0001" || fail "seed decision missing"
expect_ok "check on fresh project"     "OK" -- $PROJ check

# hooks: pre-commit embeds the absolute proj path (a bare `proj` on PATH may be
# an unrelated program), and the installed hook actually runs `proj check`
expect_ok "hooks install"              "Installed" -- $PROJ hooks install
# the hook embeds SOME absolute proj binary (the project's pixi env if it has
# one — this sandbox doesn't — else the first `proj` on PATH, e.g. a uv tool)
grep -qE 'exec "/[^"]+proj" check' .git/hooks/pre-commit && ok "hook uses absolute proj path" \
  || fail "hook missing absolute proj path"
if out=$(.git/hooks/pre-commit 2>&1) && grep -qF "OK" <<<"$out"; then ok "hook runs proj check"
else fail "hook run failed: $out"; fi

# 2. decisions (incl. supersede)
expect_ok "new decision 0002"          "Created docs/decisions/DEC-0002-" -- $PROJ decision new "Use SQLite as core store" -y
expect_ok "new decision 0003 (-s 2)"   "Created docs/decisions/DEC-0003-" -- $PROJ decision new "Adopt YAML templates" -y -s 2
expect_ok "decision supersede (-s 3)"  "Created docs/decisions/DEC-0004-" -- $PROJ decision new "Supersede via verb" -y -s 3
# supersede now renders as a blockquote line; the link text is the target's
# full filename (relative to this record's own file, same dir → bare filename)
expect_ok "supersede link rendered"    "> **supersedes:** [DEC-0002-" -- cat docs/decisions/DEC-0003-*.md
# a status legend line sits below the changelog table (decision has never
# changed status here — its transition rows are asserted below, on the task)
grep -qE '^_✏️ Draft' docs/decisions/DEC-0003-*.md \
  && ok "status legend below changelog" || fail "status legend line missing"
expect_ok "decision show"              "DEC-0001" -- $PROJ decision show DEC-0001
expect_fail "unknown verb rejected"    -- $PROJ decision explode DEC-0002

# 3. plan + task lifecycle
expect_ok "new plan 0001"              "Created docs/plans/PLN-0001-" -- $PROJ plan new "V2 rollout" -y
expect_fail "review before activate"   -- $PROJ plan review 1
expect_ok "plan activate"              "status → Active" -- $PROJ plan activate 1
TASK_ID=$($PROJ task new "Write e2e smoke script" -y --parent PLN-0001 | grep -oE '[A-Z]+-[0-9]{6}')
[ -n "$TASK_ID" ] && ok "new task $TASK_ID on plan 1" || fail "new task: no id (T-000001)"
expect_ok "task hangs under plan in tasklist" "Write e2e smoke script" -- cat TASKLIST.md
grep -qF "PLN-0001" TASKLIST.md && ok "plan node in tasklist" || fail "plan node missing in TASKLIST.md"
expect_ok "plan Task List section (type: task)" "Write e2e smoke script" -- cat docs/plans/PLN-0001-*.md

# edit task md: one step checked, one not (sync auto-promotes to In Progress)
TASK_FILE=$(ls docs/tasks/${TASK_ID}-*.md)
head -n "$(grep -n '^## Steps' "$TASK_FILE" | cut -d: -f1)" "$TASK_FILE" > .steps.tmp
printf '\n- [x] first step\n- [ ] second step\n' >> .steps.tmp
mv .steps.tmp "$TASK_FILE"
expect_ok "sync auto-promotes task"    "auto status Draft → In Progress" -- $PROJ sync
# the auto-promotion is stored in from_status/to_status (not concatenated text)
grep -qF "✏️ → 🚧 In Progress" "$TASK_FILE" \
  && ok "auto-promotion rendered from status columns" || fail "status columns not rendered"
expect_ok "task list: In Progress"     "In Progress" -- $PROJ task list
$PROJ task list | grep -qF "PLN-0001" && ok "task list shows parent" || fail "task list missing parent"
expect_fail "done with unchecked step" -- $PROJ task done $TASK_ID
sed -i 's/- \[ \] second step/- [x] second step/' "$TASK_FILE"
expect_ok "sync after all checked"     "OK" -- $PROJ sync
expect_ok "task $TASK_ID done"         "→ Done" -- $PROJ task done $TASK_ID

# 3a. summary (italic line in head fence, no "summary:" label) + fail verb
# Use the path `new` reports (not a fresh glob): on NFS the just-written file
# may not resolve through a glob yet.
SUM_OUT=$($PROJ task new "Summarized task" -y --summary "a one-line summary" 2>&1)
SUM_FILE=$(grep -oE 'docs/tasks/[A-Z]+-[0-9]{6}-[^ ]+\.md' <<<"$SUM_OUT" | head -1)
[ -n "$SUM_FILE" ] && ok "summary task created ($SUM_FILE)" || fail "summary task: no path"
grep -qF "_a one-line summary_" "$SUM_FILE" \
  && ok "summary rendered as italic line" || fail "summary italic line missing (in $SUM_FILE)"
grep -qF "> **summary:**" "$SUM_FILE" \
  && fail "summary must not be a labeled blockquote row" || ok "no 'summary:' label"
# fail verb: Any → Failed (not a done status)
DOOMED=$($PROJ task new "Doomed task" -y 2>&1 | grep -oE '[A-Z]+-[0-9]{6}' | head -1)
[ -n "$DOOMED" ] && ok "doomed task created ($DOOMED)" || fail "doomed task: no id"
expect_ok "task fail verb"             "→ Failed" -- $PROJ task fail "$DOOMED"
# 3b. template features: related (cross-type, unordered), subtask head row,
#     experiment render, deprecate verb, relation guards
SUB1=$($PROJ task new "Subtask one" -y --parent "$TASK_ID" | grep -oE '[A-Z]+-[0-9]{6}')
SUB2=$($PROJ task new "Subtask two" -y --parent "$TASK_ID" | grep -oE '[A-Z]+-[0-9]{6}')
# subtask: bare key row + a `> - [stem](file.md)` link per direct child
# (requirement 8: head refs are filename links, the editable task tree —
# with titles — stays in the contain_task record's `## Task List` section)
grep -qF "> **subtask:**" docs/tasks/${TASK_ID}-*.md && \
  grep -qF "[${SUB1}-" docs/tasks/${TASK_ID}-*.md && \
  grep -qF "[${SUB2}-" docs/tasks/${TASK_ID}-*.md \
  && ok "parent head shows subtask links" || fail "subtask head row missing"
# related: a task points at a decision AND a plan (cross-type), both directions
REL_TASK=$($PROJ task new "Related task" -y -r "DEC-0002, PLN-0001" | grep -oE '[A-Z]+-[0-9]{6}')
[ -n "$REL_TASK" ] && ok "related task created ($REL_TASK)" || fail "related task: no id"
# related (2 targets): bare key row + a `> - [filename](path)` list; the
# cross-dir link path is relative to the record's own file (../dir/filename)
grep -qF "> **related:**" docs/tasks/${REL_TASK}-*.md && \
  grep -qF "[DEC-0002-" docs/tasks/${REL_TASK}-*.md && \
  grep -qF "[PLN-0001-" docs/tasks/${REL_TASK}-*.md \
  && ok "related head row (forward)" || fail "related head row missing"
# single related: one inline line [filename](relative path)
grep -qF "> **related:** [${REL_TASK}-" docs/decisions/DEC-0002-*.md \
  && ok "related head row (reverse, on decision)" || fail "reverse related missing on decision"
grep -qF "> **related:** [${REL_TASK}-" docs/plans/PLN-0001-*.md \
  && ok "related head row (reverse, on plan)" || fail "reverse related missing on plan"
# (self-relation guard lives in the Python API Record.add_related; a `new` task
# cannot reference its own not-yet-created id, so there's no CLI path to test it)

# 3c. per-record update: -t (renames the md file) / -s / -c, verb -c after the step
expect_ok "decision update -t (rename)" "title → Renamed decision" \
  -- $PROJ decision update DEC-0002 -t "Renamed decision" -s "one-line summary" -c "Changed: refined scope"
[ -f docs/decisions/DEC-0002-renamed-decision.md ] && ok "md renamed to new slug" \
  || fail "md file not renamed after -t"
# changelog renders as a table row: | date | Category | text |
grep -qF "refined scope" docs/decisions/DEC-0002-renamed-decision.md \
  && ok "-c appended to changelog" || fail "-c missing in md"
$PROJ decision accept DEC-0002 -c "Added: accepted in review" >/dev/null \
  && grep -qF "accepted in review" docs/decisions/DEC-0002-renamed-decision.md \
  && ok "verb -c appends after the step" || fail "verb -c missing"
expect_fail "update with nothing"       -- $PROJ decision update DEC-0002
$PROJ check >/dev/null && ok "check after update/rename" || fail "check after update/rename"
expect_fail "ambiguous bare number in -r" -- $PROJ task new "Amb" -y -r 1
# experiment: its own dir + render
expect_ok "new experiment" "Created docs/experiments/EXP-0001-" -- $PROJ experiment new "Baseline run" -y
grep -qF "EXP-0001" docs/experiments/EXP-0001-*.md \
  && ok "experiment file rendered" || fail "experiment render missing"
# deprecate verb: Any → Deprecated
expect_ok "decision deprecate verb" "→ Deprecated" -- $PROJ decision deprecate DEC-0002

# 4. plan state machine
expect_ok "plan review (all done)"     "status → Pending Review" -- $PROJ plan review 1
expect_ok "plan shelve"                "status → Shelved" -- $PROJ plan shelve 1
expect_ok "plan resume"                "status → Active" -- $PROJ plan resume 1
expect_ok "plan review again"          "status → Pending Review" -- $PROJ plan review 1
expect_ok "plan archive"               "status → Archived" -- $PROJ plan archive 1

# 4b. Shelved → Archived via freeze
$PROJ plan new "Side plan" -y >/dev/null
expect_ok "plan 2 activate"            "status → Active"        -- $PROJ plan activate 2
expect_ok "plan 2 review"              "status → Pending Review" -- $PROJ plan review 2
expect_ok "plan 2 shelve"              "status → Shelved"       -- $PROJ plan shelve 2
expect_ok "plan 2 freeze"              "status → Archived"      -- $PROJ plan freeze 2
expect_fail "freeze from non-Shelved"  -- $PROJ plan freeze 1

# 5. other record_types
expect_ok "new debt"                   "Created docs/tech-debt/DEB-0001-" -- $PROJ debt new "Slow list query" -y
expect_ok "new incident"               "Created docs/incidents/INC-0001-" -- $PROJ incident new "CI flake" -y

# 6. changelog
expect_ok "changelog add"              "Added entry" -- $PROJ changelog add "Added: e2e smoke script"
expect_ok "changelog version bump"     "Released v0.1.0" -- $PROJ changelog version --bump minor
expect_ok "release-notes v0.1.0"       "e2e smoke script" -- $PROJ changelog release-notes v0.1.0
expect_ok "changelog list"             "v0.1.0" -- $PROJ changelog list
expect_ok "CHANGELOG.md rendered"      "## [v0.1.0]" -- cat CHANGELOG.md

# 7. adopt a handwritten file
cat > docs/tech-debt/DEB-0002-handwritten-debt.md <<'EOF'
# Handwritten debt

## Location

src/x.py

## Reason

legacy

## Trigger for removal

v2
EOF
expect_ok "adopt handwritten debt"     "OK" -- $PROJ adopt
expect_ok "adopted debt listed"        "DEB-0002" -- $PROJ debt list

# 8. final
expect_ok "update"                     "OK" -- $PROJ update
expect_ok "final check"                "OK" -- $PROJ check

cd - >/dev/null
echo
echo "e2e: $PASS passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
