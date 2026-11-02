"""Git hooks + merge driver: git integration for the event-log project (E4).

`proj hooks install` writes into the project's .git/:
- .gitattributes: event log merges as union, rendered md via the proj-render
  driver (keep ours, re-render after merge)
- git config merge.proj-render.{name,driver}
- pre-commit: runs `proj check`; post-merge / post-checkout / post-rewrite:
  run `proj sync --quiet` (replay + re-render; reports renumber/overwrite)

Every hook embeds the *absolute* path of the proj binary resolved at install
time — a bare `proj` on PATH may be an unrelated program (name clash), which
would run the wrong binary on every commit/merge. A hook that already exists
but is not a proj hook is never overwritten.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from proj.core import fsio
from proj.core.config import ProjConfig

#: the .gitattributes the driver + union merge rely on (C5); the event log
#: path must match events.py (.proj/events.jsonl)
GITATTRIBUTES = """\
.proj/events.jsonl  merge=union
docs/**/*.md        merge=proj-render
WIP.md              merge=proj-render
CHANGELOG.md        merge=proj-render
"""

MERGE_DRIVER_NAME = "proj: keep ours, re-render after merge"

#: post-checkout: skip a plain file checkout (identical head, $1=1 $2=$3) —
#: the guard must run BEFORE the exec below, so it is its own template
_CHECKOUT_GUARD = '[ "$1" = 1 ] && [ "$2" = "$3" ] && exit 0\n'
#: post-checkout also fires on EVERY step of a `git pull --rebase`: the sync
#: there would replay+re-render a half-rebased tree and the cache write races
#: the rebase (it can even abort it). Skip mid-rebase — post-rewrite runs once
#: when the rebase finishes. (C7, 2026-11-03: `git pull --rebase` in a collab
#: project aborted; the everyday flow is `git pull` (merge), which this keeps.)
_REBASE_GUARD = ('if [ -d "$(git rev-parse --git-dir)/rebase-merge" ] || '\
                 '[ -d "$(git rev-parse --git-dir)/rebase-apply" ]; then exit 0; fi\n')

#: {desc}/{guard}/{cmd}/{name} are filled at import; {proj} (doubled here, so
#: import-time .format() leaves a literal {proj}) is filled at install() with
#: the absolute proj binary path
_HOOK = """\
#!/bin/sh
# Installed by `proj hooks install` — {desc}
{guard}if [ -x "{{proj}}" ]; then
    PROJ_GIT_HOOK=1
    exec "{{proj}}" {cmd}
fi
echo "proj: hook target not found, skipping ({name})"
"""

PRE_COMMIT = _HOOK.format(desc="runs governance checks on every commit",
                          guard="", cmd="check", name="pre-commit")
#: the sync hooks: after a merge / a branch checkout / a rebase-rewrite the
#: working tree carries the merged event log — rebuild the cache from it and
#: re-render (E4). C4: the md is a PROJECTION of the log, so a hook-driven
#: sync must NEVER ingest (the union-merged md would be re-read and re-create
#: records the log already built — duplicates). `--rerender` does exactly the
#: rebuild-from-log + force re-render, no ingest.
POST_SYNC = _HOOK.format(desc="rebuilds the cache from the merged log and re-renders",
                         guard="", cmd="sync --rerender --quiet", name="post sync")
POST_CHECKOUT = _HOOK.format(desc="rebuilds the cache from the merged log and re-renders",
                             guard=_CHECKOUT_GUARD + _REBASE_GUARD,
                             cmd="sync --rerender --quiet", name="post checkout")

POST_HOOKS = {  # git hook name -> script
    "post-merge": POST_SYNC,
    "post-checkout": POST_CHECKOUT,
    "post-rewrite": POST_SYNC,
}


def git_in_progress(cfg: ProjConfig) -> str | None:
    """C5: refuse write commands while a git merge / rebase / cherry-pick is
    in flight (`.git/MERGE_HEAD`, `.git/REBASE_HEAD`, `.git/CHERRY_PICK_HEAD`
    present) or the worktree has unmerged paths — an event appended to a
    half-merged tree would diverge the two sides. Returns a reason, or None
    when git is idle (also None when the project is not a git checkout).
    `PROJ_GIT_HOOK=1` (set by the installed hooks) exempts the hook-driven
    `proj sync` — the post-merge / post-checkout / post-rewrite replay is the
    merge's completion handling, not a user write command."""
    if os.environ.get("PROJ_GIT_HOOK"):
        return None
    git_dir = cfg.root / ".git"
    if not git_dir.is_dir():
        return None
    for name, what in (("MERGE_HEAD", "merge"), ("REBASE_HEAD", "rebase"),
                       ("CHERRY_PICK_HEAD", "cherry-pick")):
        if (git_dir / name).exists():
            return f"a git {what} is in progress — finish it first, then retry"
    out = subprocess.run(["git", "-C", str(cfg.root), "diff", "--name-only",
                          "--diff-filter=U"],
                         check=False, capture_output=True, text=True)
    if out.stdout.strip():
        return ("unmerged paths exist — resolve the conflicts "
                "(git add / git merge --abort), then retry")
    return None


def _find_proj(root: Path) -> str:
    """Absolute path to the proj binary: the project's pixi dev env wins
    (this repo builds proj itself), then the first `proj` on PATH."""
    candidates = [root / ".pixi" / "envs" / "dev" / "bin" / "proj",
                  Path(shutil.which("proj") or "proj")]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    return str(candidates[0])


def _write_hook(hooks_dir: Path, name: str, script: str) -> None:
    hook = hooks_dir / name
    if hook.exists() and "proj hooks install" not in hook.read_text(encoding="utf-8"):
        raise ValueError(f"{hook} is not a proj hook — refusing to overwrite")
    fsio.write_atomic(hook, script)
    hook.chmod(0o755)


def _git_config(root: Path, key: str, value: str) -> None:
    subprocess.run(["git", "-C", str(root), "config", key, value],
                   check=True, capture_output=True)


def install(cfg: ProjConfig) -> str:
    git_dir = cfg.root / ".git"
    if not git_dir.is_dir():
        raise ValueError("no .git in this project — `git init` first")
    hooks_dir = git_dir / "hooks"
    proj = _find_proj(cfg.root)
    # 1: the attribute rules — tracked in the repo (the merge rules only apply
    # to committed files), so commit them on change: otherwise `hooks install`
    # leaves a dirty tree (and every clone has to commit it itself)
    attr_path = cfg.root / ".gitattributes"
    changed = not attr_path.is_file() \
        or attr_path.read_text(encoding="utf-8") != GITATTRIBUTES
    fsio.write_atomic(attr_path, GITATTRIBUTES)
    if changed:
        # commit ONLY .gitattributes (--only) — never sweep in anything the
        # user had already staged
        subprocess.run(["git", "-C", str(cfg.root), "add", ".gitattributes"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(cfg.root), "commit", "-q", "--only",
                        "-m", "proj hooks install: .gitattributes (merge rules)",
                        ".gitattributes"],
                       check=True, capture_output=True)
    # 2: the proj-render driver (keep ours; re-render does the real work)
    _git_config(cfg.root, "merge.proj-render.name", MERGE_DRIVER_NAME)
    _git_config(cfg.root, "merge.proj-render.driver", "true")
    # 3: the hooks
    _write_hook(hooks_dir, "pre-commit", PRE_COMMIT.format(proj=proj))
    for name, script in POST_HOOKS.items():
        _write_hook(hooks_dir, name, script.format(proj=proj))
    return (f"{len(POST_HOOKS) + 1} hooks, .gitattributes, "
            f"merge.proj-render driver")
