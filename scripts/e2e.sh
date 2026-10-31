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
[ -f docs/decisions/0001-record-architecture-decisions.md ] \
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
expect_ok "new decision 0002"          "Created docs/decisions/0002-" -- $PROJ new decision "Use SQLite as core store" -y
expect_ok "new decision 0003 (-s 2)"   "Created docs/decisions/0003-" -- $PROJ new decision "Adopt YAML templates" -y -s 2
expect_ok "decision new subcmd -s 3"   "Created docs/decisions/0004-" -- $PROJ decision new "Supersede via subcommand" -y -s 3
# supersede now renders as a blockquote line with a markdown link to the target's md
expect_ok "supersede subcmd link"      "> **supersedes:** [0003](" -- cat docs/decisions/0004-*.md
expect_ok "supersede link rendered"    "> **supersedes:** [0002](" -- cat docs/decisions/0003-*.md
expect_ok "decision toc"               "Wrote" -- $PROJ decision toc
grep -qF "0003" docs/decisions/_TOC.md && ok "toc lists 0003" || fail "toc missing 0003"
grep -qF "| 0001 | ✅ |" docs/decisions/_TOC.md \
  && ok "toc status emoji-only" || fail "toc status not emoji-only"
grep -qF "* 🌱 Proposed · ✅ Accepted · 📛 Deprecated" \
  docs/decisions/_TOC.md && ok "toc one-line legend" || fail "toc missing one-line legend"
expect_ok "decision graph"             "D0002 --> D0003" -- $PROJ decision graph
expect_fail "bogus status rejected"    -- $PROJ status decision 0002 Bogus

# 3. plan + task lifecycle
expect_ok "new plan 0001"              "Created docs/plans/0001-" -- $PROJ plan new "V2 rollout" -y
expect_fail "review before activate"   -- $PROJ plan review 1
expect_ok "plan activate"              "status → Active" -- $PROJ plan activate 1
expect_ok "new task A on plan 1"       "Created tasks/A-" -- $PROJ new task "Write e2e smoke script" -y --parent 1
expect_ok "task hangs under plan in tasklist" "Write e2e smoke script" -- cat TASKLIST.md
grep -qE "plan 0[0-9]+" TASKLIST.md && ok "plan node in tasklist" || fail "plan node missing in TASKLIST.md"
expect_ok "plan Task List section (type: task)" "Write e2e smoke script" -- cat docs/plans/0001-*.md

# edit task md: one step checked, one not (sync auto-promotes to In Progress)
TASK_FILE=$(ls tasks/A-*.md)
head -n "$(grep -n '^## Steps' "$TASK_FILE" | cut -d: -f1)" "$TASK_FILE" > .steps.tmp
printf '\n- [x] first step\n- [ ] second step\n' >> .steps.tmp
mv .steps.tmp "$TASK_FILE"
expect_ok "sync auto-promotes task"    "auto status Not Started → In Progress" -- $PROJ sync
expect_ok "task list: In Progress"     "In Progress" -- $PROJ task list
$PROJ task list | grep -qF "(parent 1)" && ok "task list shows parent" || fail "task list missing (parent 1)"
expect_fail "done with unchecked step" -- $PROJ task done A
sed -i 's/- \[ \] second step/- [x] second step/' "$TASK_FILE"
expect_ok "sync after all checked"     "OK" -- $PROJ sync
expect_ok "task A done"                "task A → Done" -- $PROJ task done A

# 4. plan state machine
expect_ok "plan review (all done)"     "status → Pending Review" -- $PROJ plan review 1
expect_ok "plan shelve"                "status → Shelved" -- $PROJ plan shelve 1
expect_ok "plan resume"                "status → Active" -- $PROJ plan resume 1
expect_ok "plan review again"          "status → Pending Review" -- $PROJ plan review 1
expect_ok "plan archive"               "status → Archived" -- $PROJ plan archive 1

# 4b. Shelved → Archived via freeze
$PROJ new plan "Side plan" -y >/dev/null
expect_ok "plan 2 activate"            "status → Active"        -- $PROJ plan activate 2
expect_ok "plan 2 review"              "status → Pending Review" -- $PROJ plan review 2
expect_ok "plan 2 shelve"              "status → Shelved"       -- $PROJ plan shelve 2
expect_ok "plan 2 freeze"              "status → Archived"      -- $PROJ plan freeze 2
expect_fail "freeze from non-Shelved"  -- $PROJ plan freeze 1

# 5. other record_types
expect_ok "new debt"                   "Created docs/tech-debt/0001-" -- $PROJ new debt "Slow list query" -y
expect_ok "new incident"               "Created docs/incidents/0001-" -- $PROJ new incident "CI flake" -y

# 6. changelog
expect_ok "changelog add"              "Added entry" -- $PROJ changelog add "Added: e2e smoke script"
expect_ok "changelog version bump"     "Released v0.1.0" -- $PROJ changelog version --bump minor
expect_ok "release-notes v0.1.0"       "e2e smoke script" -- $PROJ changelog release-notes v0.1.0
expect_ok "changelog list"             "v0.1.0" -- $PROJ changelog list
expect_ok "CHANGELOG.md rendered"      "## [v0.1.0]" -- cat CHANGELOG.md

# 7. adopt a handwritten file
cat > docs/tech-debt/0002-handwritten-debt.md <<'EOF'
# 0002 Handwritten debt

## Location

src/x.py

## Reason

legacy

## Trigger for removal

v2
EOF
expect_ok "adopt handwritten debt"     "OK" -- $PROJ adopt
expect_ok "adopted debt listed"        "0002" -- $PROJ list debt

# 8. final
expect_ok "update"                     "OK" -- $PROJ update
expect_ok "final check"                "OK" -- $PROJ check

cd - >/dev/null
echo
echo "e2e: $PASS passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
