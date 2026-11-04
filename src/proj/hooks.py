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
from proj.core.db import ProjError

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


# P4: per-command cache of the git-state check, keyed by the git dir's STATE
# SIGNATURE (mtimes + sizes of the merge/rebase/cherry-pick marker files, plus
# the unmerged count). A write command calls `db.begin()` 1-2 times and each
# call used to spawn a `git rev-parse` + `git diff` subprocess pair; the
# signature is microsecond stat calls, and ANY change of git state (a marker
# created or removed, a conflict resolved) changes it, so a stale "in
# progress" can never outlive the state that produced it. (A time-based TTL
# would not — a test or a user aborting a merge within it would be refused.)
_GIT_STATE_CACHE: dict[Path, tuple[tuple, str | None]] = {}


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
    # review 2026-11-05 #6: the REAL git dir — `cfg.root / ".git"` misses a
    # git WORKTREE (.git is a file pointing at .git/worktrees/<name>/) and a
    # project in a MONOREPO subdirectory (no .git at the project root), and
    # both silently returned None (C5 off) while a merge was in flight.
    # `git rev-parse --git-dir` resolves both (a relative path, or "." for a
    # bare checkout); the project root IS the checkout/worktree root.
    out = subprocess.run(["git", "-C", str(cfg.root), "rev-parse", "--git-dir"],
                         check=False, capture_output=True, text=True)
    if out.returncode != 0:
        return None  # not a git checkout — C5 has nothing to guard here
    git_dir = (cfg.root / out.stdout.strip()).resolve()
    # P4: a microsecond-precision stat signature — the mtimes of the three
    # marker files (None when absent) plus the index. Every git-state change
    # (merge/rebase/cherry-pick starts or ends, a conflict is resolved or
    # aborted) creates/removes/rewrites one of them, so a cache entry can
    # never outlive the state it describes (a time TTL could: an abort within
    # it would still refuse).
    markers = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD")
    sig = tuple(
        (git_dir / f).stat().st_mtime_ns if (git_dir / f).is_file() else None
        for f in (*markers, "index"))
    hit = _GIT_STATE_CACHE.get(git_dir)
    if hit is not None and hit[0] == sig:
        return hit[1]
    reason: str | None = None
    for name, what in zip(markers, ("merge", "rebase", "cherry-pick"), strict=True):
        if (git_dir / name).exists():
            reason = f"a git {what} is in progress — finish it first, then retry"
            break
    if reason is None:
        out = subprocess.run(["git", "-C", str(cfg.root), "diff", "--name-only",
                              "--diff-filter=U"],
                             check=False, capture_output=True, text=True)
        if out.returncode != 0:
            # P1-10: a failed git (broken .git, missing binary) must not read as
            # "idle" — refusing the write is the safe direction
            reason = (f"git diff failed (rc {out.returncode}) — the repository "
                      "seems broken; fix it, then retry")
        elif out.stdout.strip():
            reason = ("unmerged paths exist — resolve the conflicts "
                      "(git add / git merge --abort), then retry")
    _GIT_STATE_CACHE[git_dir] = (sig, reason)
    return reason


def merge_resolve(cfg: ProjConfig) -> str:
    """B-4 (2026-11-03): resolve a merge that was ABORTED on a rename/rename
    conflict of record md files — the case the proj-render driver never sees
    (git halts before any merge driver runs). Both sides changed the SAME
    record's title → each sync renamed `<ID>-<old>.md` to a different slug →
    `CONFLICT (rename/rename)`.

    The unmerged index (`git ls-files -u`) says exactly who is who: stage 1 =
    base (the original name), stage 2 = ours, stage 3 = theirs. Grouped by
    the record id the filename encodes, ≥ 2 unmerged names of the SAME id is
    a rename/rename conflict: the STAGE-2 (ours) name is kept — in the
    everyday `git pull` flow ours is the later writer (LWW) — the stage-3
    name and the stage-1 original are removed, the survivor staged, the merge
    committed. (The caller then runs `sync --rerender --quiet`, exactly like
    the post-merge hook.) Anything that is not a same-id record conflict is
    left for the user — the command reports it and refuses to commit."""
    from proj.core.templates import load_registry
    from proj.engine.store import _id_from_name

    git_dir = cfg.root / ".git"
    if not (git_dir / "MERGE_HEAD").exists():
        raise ProjError("no merge in progress — nothing to resolve")
    # `-z`: NUL-separated records, the path RAW (no C-quoting of non-ASCII)
    out = subprocess.run(["git", "-C", str(cfg.root), "ls-files", "-u", "-z"],
                         check=False, capture_output=True)
    if out.returncode != 0:
        raise ProjError(f"git ls-files failed (rc {out.returncode}) — the "
                        "repository seems broken; fix it, then retry")
    # each record: `<mode> <sha> <stage>\t<path>\0`; the stage (1 = base,
    # 2 = ours, 3 = theirs) is the 3rd whitespace token
    entries: dict[tuple[int, str], None] = {}
    for rec in out.stdout.split(b"\0"):
        if not rec:
            continue
        meta, sep, path = rec.partition(b"\t")
        if not sep:
            continue  # not `<mode> <sha> <stage>\t<path>` — nothing to act on
        tok = meta.split()
        if len(tok) < 3:
            continue
        entries.setdefault((int(tok[2]), path.decode("utf-8", "replace")), None)
    unmerged = sorted({p for st, p in entries if st in (1, 2, 3)})
    if not unmerged:
        raise ProjError("no unmerged paths — the merge is not stuck on "
                        "conflicts (nothing to resolve)")
    reg = load_registry(cfg.root)
    # group the .md paths by the record id their NAME encodes (the marker
    # uuid is the real identity, F1 — the id prefix is enough to group)
    groups: dict[str, set[str]] = {}
    for name in unmerged:
        spec = next((s for s in reg.values()
                     if name.endswith(".md") and _id_from_name(Path(name), s)),
                    None)
        if spec is not None:
            groups.setdefault(_id_from_name(Path(name), spec), set()).add(name)
    # a rename/rename conflict = ≥ 2 unmerged names of the SAME id, one of
    # which is the OURS (stage-2) name. Everything else — a non-record path,
    # a single-name group, a same-id group with no ours side — is NOT resolved
    # here: it would leave the merge un-committable (git commit refuses while
    # unmerged paths remain), so we refuse instead of half-resolving.
    conflicts: dict[str, set[str]] = {}
    for mid, names in groups.items():
        if len(names) >= 2 and any(n in names for st, n in entries if st == 2):
            conflicts[mid] = names
    in_conflict: set[str] = (set().union(*conflicts.values())
                             if conflicts else set())
    unhandled = [n for n in unmerged if n not in in_conflict]
    if not conflicts:
        raise ProjError(
            "no record rename/rename conflict among the unmerged paths — "
            f"resolve manually (git add / git rm): {', '.join(unmerged)}")
    if unhandled:
        raise ProjError(
            "the unmerged paths mix record rename/rename conflicts with "
            "something this command will not touch — resolve those manually "
            f"(git add / git rm): {', '.join(unhandled)}")
    kept: list[str] = []
    for names in sorted(conflicts.values()):
        # the STAGE-2 (ours) name is the LWW survivor; the stage-3 name and
        # the stage-1 original are dropped (F1 keeps exactly ours' canonical
        # file once the merge is committed)
        survivor = next(n for st, n in entries if n in names and st == 2)
        kept.append(survivor)
        _git_run(cfg, ["add", "--", survivor])
        for other in sorted(names - {survivor}):
            _git_run(cfg, ["rm", "-q", "-f", "--", other])
    _git_run(cfg, ["commit", "-m", "merge: proj merge resolve (B-4: "
               "rename/rename conflicts of record files, keep the LWW name)"])
    return "merge resolved: kept " + ", ".join(kept)


def _git_run(cfg: ProjConfig, args: list[str]) -> None:
    out = subprocess.run(["git", "-C", str(cfg.root), *args],
                         check=False, capture_output=True, text=True)
    if out.returncode != 0:
        raise ProjError(f"git {' '.join(args)} failed: "
                        f"{(out.stderr or out.stdout).strip()}")


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
        # P1-9: --no-verify — a pre-commit hook ALREADY installed by an earlier
        # `hooks install` would run `proj check` on THIS commit; a failing check
        # then aborted the install mid-way (dirty tree, half-installed hooks)
        subprocess.run(["git", "-C", str(cfg.root), "commit", "-q", "--only",
                        "--no-verify",
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
