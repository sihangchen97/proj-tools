#!/usr/bin/env bash
# e2e smoke test: full proj CLI lifecycle in the .testproj sandbox.
# Usage: pixi run e2e   (or: bash scripts/e2e.sh)
set -u
cd "$(dirname "$0")/.."

# configurable so parallel agents can each run e2e in their own sandbox:
# PROJ_E2E_SANDBOX=.testproj-agent-b pixi run e2e
SANDBOX=${PROJ_E2E_SANDBOX:-.testproj}
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
expect_ok "new decision 0003 (--supersede 2)" "Created docs/decisions/DEC-0003-" -- $PROJ decision new "Adopt YAML templates" -y --supersede 2
expect_ok "decision supersede (--supersede 3)" "Created docs/decisions/DEC-0004-" -- $PROJ decision new "Supersede via verb" -y --supersede 3
# supersede now renders as a blockquote line; the link text is the target's
# full filename (relative to this record's own file, same dir → bare filename)
expect_ok "supersede link rendered"    "> **supersedes:** [DEC-0002-" -- cat docs/decisions/DEC-0003-*.md
# the status legend (emoji → name) sits inside the changelog <details>, at the
# bottom (these decisions only have the "created" row — no status change yet)
grep -qF '<details>' docs/decisions/DEC-0003-*.md \
  && grep -qF '✏️ Draft · 🌱 Proposed' docs/decisions/DEC-0003-*.md \
  && ok "status legend inside changelog details" || fail "legend missing in details"
expect_ok "decision show"              "DEC-0001" -- $PROJ decision show DEC-0001
expect_fail "unknown verb rejected"    -- $PROJ decision explode DEC-0002

# 3. plan + task lifecycle
expect_ok "new plan 0001"              "Created docs/plans/PLN-0001-" -- $PROJ plan new "V2 rollout" -y
PLAN_FILE=$(ls docs/plans/PLN-0001-*.md)
# hand-write the Goal section (user-owned): re-renders must keep it
python3 - "$PLAN_FILE" <<'PY'
import sys
p = sys.argv[1]
t = open(p).read()
marker = "## Goal\n\n"
i = t.index(marker) + len(marker)
t = t[:i] + "Owner: e2e · Target: W2\nShip V2 to all users.\n\n" + t[i:]
open(p, "w").write(t)
PY
expect_fail "review before activate"   -- $PROJ plan review 1
expect_ok "plan activate"              "status → Active" -- $PROJ plan activate 1
# TASKLIST membership is DERIVED (no registration): a Draft plan's tasks are
# hidden; once Active, the plan shows while it has an unfinished descendant task
grep -qF "PLN-0001" TASKLIST.md && fail "draft plan in tasklist" \
  || ok "draft plan absent from tasklist"
TASK_ID=$($PROJ task new "Write e2e smoke script" -y --parent PLN-0001 | grep -oE '[A-Z]+-[0-9]{6}')
[ -n "$TASK_ID" ] && ok "new task $TASK_ID on plan 1 (T-000001: no T-000000 seed)" || fail "new task: no id"
# the plan's attached-task tree: no ## heading of its own, a checkbox + a
# hyperlink per task (text = the task's filename, path relative to the plan)
grep -qE "\[${TASK_ID}-[a-z0-9-]+\]\(\.\./tasks/${TASK_ID}-[a-z0-9-]+\.md\)" "$PLAN_FILE" \
  && ok "plan tree links the task (checkbox + hyperlink)" \
  || fail "plan tree has no task hyperlink (expected [$TASK_ID-…](../tasks/…))"
grep -qF "## Task List" "$PLAN_FILE" \
  && fail "plan tree must not carry a ## heading" || ok "no ## heading on the task tree"
# now Active + the task unfinished → the plan shows in the global TASKLIST
grep -qF "PLN-0001" TASKLIST.md && ok "plan node in tasklist (derived)" \
  || fail "plan node missing in TASKLIST.md"
expect_ok "task hangs under plan in tasklist" "$TASK_ID" -- cat TASKLIST.md
# a task made WITHOUT --parent sits under the derived Unparented section
UNPARENTED=$($PROJ task new "Ad-hoc task" -y | grep -oE '[A-Z]+-[0-9]{6}')
grep -qF "## Unparented" TASKLIST.md && grep -qF "$UNPARENTED" TASKLIST.md \
  && ok "unparented section holds the ad-hoc task" || fail "Unparented section missing"
# the descendant-task gate: a parent task cannot go Done while a child is not
# in a terminal status; Done or Failed both unblock it
SUB=$($PROJ task new "Sub-step" -y --parent "$TASK_ID" | grep -oE '[A-Z]+-[0-9]{6}')
expect_fail "done with unfinished child" -- $PROJ task done "$TASK_ID"
# plan review is blocked by an unfinished GRANDCHILD (gates cover the whole tree)
expect_fail "review blocked by grandchild" -- $PROJ plan review 1
expect_ok "child done"                 "→ Done" -- $PROJ task done "$SUB"
expect_ok "parent done (gate passed)"  "→ Done" -- $PROJ task done "$TASK_ID"
# task move: re-home the Unparented ad-hoc task under a fresh task, then detach
MOVE_TARGET=$($PROJ task new "Move target" -y | grep -oE '[A-Z]+-[0-9]{6}')
expect_ok "task move re-homes"         "parent → $MOVE_TARGET" -- $PROJ task move "$UNPARENTED" --parent "$MOVE_TARGET"
grep -qF "$MOVE_TARGET" TASKLIST.md && grep -qF "$UNPARENTED" TASKLIST.md \
  && ok "moved task under its new parent in tasklist" || fail "moved task not under new parent"
expect_ok "task move detaches"         "parent → none" -- $PROJ task move "$UNPARENTED" --detach
# a real cycle: P → C, then `move P --parent C` must fail (C is P's descendant)
PARENT3=$($PROJ task new "Cycle top" -y | grep -oE '[A-Z]+-[0-9]{6}')
CHILD3=$($PROJ task new "Cycle child" -y --parent "$PARENT3" | grep -oE '[A-Z]+-[0-9]{6}')
expect_fail "move under own descendant (cycle)" -- $PROJ task move "$PARENT3" --parent "$CHILD3"
expect_fail "move under itself"              -- $PROJ task move "$PARENT3" --parent "$PARENT3"
expect_fail "move to ghost parent"     -- $PROJ task move "$UNPARENTED" --parent PLN-9999
expect_fail "move with both flags"     -- $PROJ task move "$UNPARENTED" --parent "$MOVE_TARGET" --detach
# a Failed (terminal) child unblocks the plan's review gate (failed ≠ done,
# but the tree is "finished"); the plan then goes Pending Review
DOOMED=$($PROJ task new "Doomed task" -y --parent PLN-0001 | grep -oE '[A-Z]+-[0-9]{6}')
expect_ok "task fail verb"             "→ Failed" -- $PROJ task fail "$DOOMED"
expect_ok "review (tree finished)"     "status → Pending Review" -- $PROJ plan review 1
# every descendant task of the plan is now terminal (Done/Failed) → the plan's
# section leaves TASKLIST.md again
grep -qF "PLN-0001" TASKLIST.md && fail "finished plan still in tasklist" \
  || ok "finished plan gone from tasklist"
# re-open restriction: the plan is Pending Review (gates.tasks) → a child task
# cannot be re-opened, nor re-parented / moved there (DOOMED is Failed)
expect_fail "start under closed plan"  -- $PROJ task start "$DOOMED"
expect_fail "new task under closed plan" -- $PROJ task new "Fresh" -y --parent PLN-0001
expect_fail "move task under closed plan" -- $PROJ task move "$UNPARENTED" --parent PLN-0001

# 3d. items: the task Acceptance Criteria checklist lifecycle (engine/items.py)
ITASK=$($PROJ task new "Items task" -y | grep -oE '[A-Z]+-[0-9]{6}')
expect_ok "item add #001"            "#001" -- $PROJ item add "$ITASK" "tests pass"
expect_ok "item add #002"            "#002" -- $PROJ item add "$ITASK" "docs updated"
expect_ok "item group add A"         "### A" -- $PROJ item group add "$ITASK" "Checks"
expect_ok "item add to group A01"    "#A01" -- $PROJ item add "$ITASK" "sub-detail" --group A
expect_ok "item ls (open)"          "$ITASK#001" -- $PROJ item ls "$ITASK" --open
# the first closed item promotes Draft → In Progress (the task profile's promote)
expect_ok "item check #001"          "- [x] #001" -- $PROJ item check "$ITASK#001"
$PROJ task show "$ITASK" | grep -qF "status: In Progress" \
  && ok "first check promoted Draft → In Progress" || fail "no auto-promote on first check"
# gates.items: done is blocked while any criterion is still unclosed
expect_fail "done blocked by open item" -- $PROJ task done "$ITASK"
$PROJ item check "${ITASK}#002" >/dev/null
$PROJ item check "${ITASK}#A01" >/dev/null
expect_ok "done after all items closed" "→ Done" -- $PROJ task done "$ITASK"
# numbers are monotonic per record: deleting #002 then adding yields #003, not #002
ITASK2=$($PROJ task new "Items task 2" -y | grep -oE '[A-Z]+-[0-9]{6}')
$PROJ item add "$ITASK2" "one" >/dev/null
$PROJ item add "$ITASK2" "two" >/dev/null
$PROJ item rm "${ITASK2}#002" >/dev/null
expect_ok "number not reused after rm" "#003" -- $PROJ item add "$ITASK2" "three"
# a cross-group move gets a FRESH number (#003 → #A01); the old one is void
$PROJ item group add "$ITASK2" "Later" >/dev/null
expect_ok "item mv cross-group renumbers" "#A01" -- $PROJ item mv "${ITASK2}#003" --group A
expect_fail "old number void after move" -- $PROJ item check "${ITASK2}#003"
expect_ok "item mv to first"           "#001" -- $PROJ item mv "${ITASK2}#001" --first
expect_fail "bad item ref"             -- $PROJ item check "${ITASK2}#99"
expect_fail "unknown item state"       -- $PROJ item set "${ITASK2}#001" nosuch
$PROJ check >/dev/null && ok "check with items" || fail "check with items"

# 3a0. Tasks block coexistence: hand-written lines + task links with trailing
# description coexist; a task line's checkbox is driven by the DB (a hand-ticked
# box is corrected on sync, `task done` flips it, the trailing text stays);
# the block never blocks the review gate (it lists delegated tasks, not steps)
PLAN2_FILE=$($PROJ plan new "Coexist plan" -y | grep -oE 'docs/plans/[A-Z]+-[0-9]{4,6}-[^ ]+\.md')
CT1=$($PROJ task new "Coexist A" -y --parent PLN-0002 | grep -oE '[A-Z]+-[0-9]{6}')
CT2=$($PROJ task new "Coexist B" -y --parent PLN-0002 | grep -oE '[A-Z]+-[0-9]{6}')
python3 - "$PLAN2_FILE" "$CT1" <<'PY'
import sys
p, t1 = sys.argv[1], sys.argv[2]
t = open(p).read()
i, j = t.find("<details open>"), t.find("</details>", t.find("<details open>")) + len("</details>")
open(p, "w").write(t[:i] + (
    "<details open>\n<summary>Tasks</summary>\n\n"
    f"- [x] [{t1}-coexist-a](../tasks/{t1}-coexist-a.md)  shipped already\n"
    "- [ ] a plain hand-written line, not a task\n"
    "</details>") + t[j:])
PY
$PROJ sync >/dev/null
grep -qF "a plain hand-written line, not a task" "$PLAN2_FILE" && ok "hand line kept in Tasks block" \
  || fail "hand line lost after sync"
grep -qF "shipped already" "$PLAN2_FILE" && ok "trailing text kept on the task line" \
  || fail "trailing text lost after sync"
grep -qF -- "- [ ] [$CT1-" "$PLAN2_FILE" && ok "task checkbox follows DB (hand tick corrected)" \
  || fail "task checkbox not corrected to [ ] after sync"
expect_ok "task done flips the line" "→ Done" -- $PROJ task done "$CT1"
grep -qF -- "- [x] [$CT1-" "$PLAN2_FILE" && grep -qF "shipped already" "$PLAN2_FILE" && \
  ok "done flips the line, text intact" || fail "done line / text mangled"
grep -qF "a plain hand-written line, not a task" "$PLAN2_FILE" \
  && ok "hand line still there" || fail "hand line lost after task done"
# the block's [ ] boxes must not block the plan gate: with CT2 done, review passes
$PROJ task done "$CT2" >/dev/null
$PROJ plan activate 2 >/dev/null
expect_ok "review not blocked by Tasks-block boxes" "status → Pending Review" -- $PROJ plan review 2

# 3a. summary (italic line in head fence, no "summary:" label)
# Use the path `new` reports (not a fresh glob): on NFS the just-written file
# may not resolve through a glob yet.
SUM_OUT=$($PROJ task new "Summarized task" -y --summary "a one-line summary" 2>&1)
SUM_FILE=$(grep -oE 'docs/tasks/[A-Z]+-[0-9]{6}-[^ ]+\.md' <<<"$SUM_OUT" | head -1)
[ -n "$SUM_FILE" ] && ok "summary task created ($SUM_FILE)" || fail "summary task: no path"
grep -qF "_a one-line summary_" "$SUM_FILE" \
  && ok "summary rendered as italic line" || fail "summary italic line missing (in $SUM_FILE)"
grep -qF "> **summary:**" "$SUM_FILE" \
  && fail "summary must not be a labeled blockquote row" || ok "no 'summary:' label"
# 3b. template features: related (cross-type, unordered), subtask head row,
#     experiment render, deprecate verb, relation guards
# (a FRESH task as the tree root — the old $TASK_ID is Done, a closed state)
PARENT2=$($PROJ task new "Tree parent" -y | grep -oE '[A-Z]+-[0-9]{6}')
SUB1=$($PROJ task new "Subtask one" -y --parent "$PARENT2" | grep -oE '[A-Z]+-[0-9]{6}')
SUB2=$($PROJ task new "Subtask two" -y --parent "$PARENT2" | grep -oE '[A-Z]+-[0-9]{6}')
# sub-tasks render as a <details open> tree titled "Tasks" under the task's own
# head fence (no subtask head row, no ## heading); any depth, fully expanded
grep -qF "<summary>Tasks</summary>" docs/tasks/${PARENT2}-*.md && \
  grep -qF "<details open>" docs/tasks/${PARENT2}-*.md && \
  grep -qF "[${SUB1}-" docs/tasks/${PARENT2}-*.md && \
  grep -qF "[${SUB2}-" docs/tasks/${PARENT2}-*.md \
  && ok "parent md shows subtask tree" || fail "subtask tree missing in task md"
grep -qF "> **subtask:**" docs/tasks/${PARENT2}-*.md \
  && fail "subtask head row must be gone" || ok "no subtask head row"
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

# 3c. per-record update: -t (renames the md file) / -s / -m, verb -m after the step
expect_ok "decision update -t (rename)" "title → Renamed decision" \
  -- $PROJ decision update DEC-0002 -t "Renamed decision" -s "one-line summary" -m "Changed: refined scope"
[ -f docs/decisions/DEC-0002-renamed-decision.md ] && ok "md renamed to new slug" \
  || fail "md file not renamed after -t"
grep -qF "refined scope" docs/decisions/DEC-0002-renamed-decision.md \
  && ok "-m appended to changelog" || fail "-m missing in md"
$PROJ decision accept DEC-0002 -m "Added: accepted in review" >/dev/null \
  && grep -qF "accepted in review" docs/decisions/DEC-0002-renamed-decision.md \
  && ok "verb -m appends after the step" || fail "verb -m missing"
expect_fail "update with nothing"       -- $PROJ decision update DEC-0002
$PROJ check >/dev/null && ok "check after update/rename" || fail "check after update/rename"
expect_fail "ambiguous bare number in -r" -- $PROJ task new "Amb" -y -r 1
# experiment: its own dir + render
expect_ok "new experiment" "Created docs/experiments/EXP-0001-" -- $PROJ experiment new "Baseline run" -y
grep -qF "EXP-0001" docs/experiments/EXP-0001-*.md \
  && ok "experiment file rendered" || fail "experiment render missing"
# deprecate verb: Any → Deprecated
expect_ok "decision deprecate verb" "→ Deprecated" -- $PROJ decision deprecate DEC-0002

# 4. plan state machine (plan 1 is already Pending Review from the
# failed-child gate check above; the tree is fully terminal so the gates pass)
expect_ok "plan shelve"                "status → Shelved" -- $PROJ plan shelve 1
expect_ok "plan resume"                "status → Active" -- $PROJ plan resume 1
expect_ok "plan review again"          "status → Pending Review" -- $PROJ plan review 1
expect_ok "plan archive"               "status → Archived" -- $PROJ plan archive 1
# re-open restriction also holds in Archived (gates.tasks)
expect_fail "start a task under archived plan" \
  -- $PROJ task new "Too late" -y --parent PLN-0001

# 4b. Shelved → Archived via freeze
$PROJ plan new "Side plan" -y >/dev/null
expect_ok "plan 3 activate"            "status → Active"        -- $PROJ plan activate 3
expect_ok "plan 3 review"              "status → Pending Review" -- $PROJ plan review 3
expect_ok "plan 3 shelve"              "status → Shelved"       -- $PROJ plan shelve 3
expect_ok "plan 3 freeze"              "status → Archived"      -- $PROJ plan freeze 3
expect_fail "freeze from non-Shelved"  -- $PROJ plan freeze 1

# 4c. items: plan Success Criteria gate (gates.items on Archived) — review passes
# with criteria pending (gates.tasks only), but archive is blocked until each is
# evaluated (met / missed); shelve has no item gate and still succeeds
PLN4_FILE=$($PROJ plan new "Items plan" -y | grep -oE 'docs/plans/[A-Z]+-[0-9]{4,6}-[^ ]+\.md')
$PROJ plan activate 4 >/dev/null
expect_ok "item add to plan"           "#001" -- $PROJ item add PLN-0004 "error rate < 0.1%"
expect_ok "item add to plan #002"       "#002" -- $PROJ item add PLN-0004 "3 pilots onboarded"
$PROJ plan review 4 >/dev/null
expect_ok "item set met"               "✅" -- $PROJ item set "PLN-0004#001" met --note "14 days green"
expect_ok "item set missed"            "❌" -- $PROJ item set "PLN-0004#002" missed --note "only 2 onboarded"
# gates.items: archive is blocked while a criterion is still pending (#003 is the 3rd added)
expect_ok "item add pending #003"       "#003" -- $PROJ item add PLN-0004 "p99 under 1s"
expect_fail "archive blocked by pending item" -- $PROJ plan archive 4
# shelve has NO item gate (pausing doesn't require the list closed)
expect_ok "shelve with a pending item" "status → Shelved" -- $PROJ plan shelve 4
$PROJ plan resume 4 >/dev/null && $PROJ plan review 4 >/dev/null
$PROJ item set "PLN-0004#003" missed >/dev/null
expect_ok "archive after all items evaluated" "status → Archived" -- $PROJ plan archive 4
# re-open restriction: an Archived plan can't gain an unclosed item
expect_fail "uncheck on archived plan" -- $PROJ item uncheck "PLN-0004#001"

# 5. other record_types
expect_ok "new debt"                   "Created docs/tech-debt/DEB-0001-" -- $PROJ debt new "Slow list query" -y
expect_ok "new incident"               "Created docs/incidents/INC-0001-" -- $PROJ incident new "CI flake" -y
# 5b. items: incident Action Items carry owner/due as fields (not free text);
# --overdue filters on a past due date, and a closed item stops being overdue
expect_ok "incident item owner+due"    "· owner: sara" \
  -- $PROJ item add INC-0001 "add alert" --owner sara --due 2026-10-01
expect_ok "incident item overdue"      "INC-0001#001" -- $PROJ item ls --overdue
$PROJ item check "INC-0001#001" >/dev/null
! $PROJ item ls --overdue | grep -qF "INC-0001#001" && ok "closed item not overdue" \
  || fail "closed item still listed as overdue"

# 5a. TASKLIST membership is derived: the experiment (past Draft, unfinished
# task) shows up automatically; a Draft debt without tasks shows nothing; a
# record whose tasks all finish disappears again
$PROJ experiment propose 1 >/dev/null
EXP_TASK=$($PROJ task new "Run baseline" -y --parent EXP-0001 | grep -oE '[A-Z]+-[0-9]{6}')
grep -qF "EXP-0001" TASKLIST.md && ok "experiment node in tasklist (derived)" \
  || fail "experiment missing from TASKLIST.md"
grep -qF "DEB-0001" TASKLIST.md && fail "draft debt with no tasks in tasklist" \
  || ok "draft debt absent from tasklist"
$PROJ task done "$EXP_TASK" >/dev/null
grep -qF "EXP-0001" TASKLIST.md && fail "finished experiment still in tasklist" \
  || ok "finished experiment gone from tasklist"
$PROJ experiment list | grep -qF "EXP-0001" && ok "experiment record kept" \
  || fail "the list removed the record"
expect_fail "tasklist commands are gone" -- $PROJ tasklist add EXP-0001

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

# 7b. P0-1 regression: a bare number that fits SEVERAL record types must be
# rejected (DEC-0001 / T-000001 / PLN-0001 … all exist by now) — no "first
# type wins"; a unique zero-shortened form ("5" → T-000005) still resolves.
expect_fail "ambiguous bare sequence rejected" -- $PROJ task new "Amb id" -y --parent 1
OUT=$($PROJ task new "Ok id" -y --parent 5 2>&1)
TOK=$(grep -oE 'T-[0-9]{6}' <<<"$OUT" | head -1)
grep -qF "T-000005" "docs/tasks/${TOK}-"*.md \
  && ok "unique bare parent resolves to T-000005" || fail "unique bare parent not resolved: $OUT"
$PROJ check >/dev/null && ok "check after ambiguous-ref tests" || fail "check after ambiguous-ref tests"

# 8. final
expect_ok "update"                     "OK" -- $PROJ update
expect_ok "final check"                "OK" -- $PROJ check

cd - >/dev/null
echo
echo "e2e: $PASS passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
