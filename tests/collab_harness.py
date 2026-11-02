"""Two-clone git-collab harness (hard-parts-impl E3 / C7).

Bare repo + two clones of a real proj project, with helpers to run the real
`proj` binary in a clone, push/pull, and byte-compare the mergeable project
data (all `*.md` + `proj ls --json` / `proj item ls --json`).

This module is deliberately pytest-free: it is a plain helper library. The
`two_clones` pytest fixture (and the xfail-marked scenarios) live in
`test_collab.py`.

The proj binary is resolved the way `scripts/e2e.py` does: this repo's pixi
dev env wins (it builds proj itself), then the first `proj` on PATH.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent  # the proj source repo


def _git(*args: str, cwd: Path) -> str:
    p = subprocess.run(["git", *args], check=False, cwd=str(cwd),
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}:\n{p.stdout}\n{p.stderr}")
    return p.stdout


def _configure(cfg: Path) -> None:
    _git("config", "user.email", "proj@test", cwd=cfg)
    _git("config", "user.name", "proj", cwd=cfg)


def _find_proj() -> str:
    """The real `proj` binary: this repo's pixi dev env (it builds proj),
    else the first `proj` on PATH."""
    env_proj = _REPO / ".pixi" / "envs" / "dev" / "bin" / "proj"
    if env_proj.is_file():
        return str(env_proj)
    out = subprocess.run(["sh", "-c", "command -v proj"], check=False,
                         capture_output=True, text=True).stdout.strip()
    return out or "proj"


@dataclass
class Clone:
    path: Path
    proj: str  # the proj binary to run inside it
    hooks: bool = False  # whether `proj hooks install` succeeded (E4)

    def run(self, *args: str, expect: int = 0) -> str:
        """Run a real `proj` command in this clone (cwd = the clone)."""
        p = subprocess.run([self.proj, *args], check=False, cwd=str(self.path),
                           capture_output=True, text=True)
        if p.returncode != expect:
            raise AssertionError(
                f"proj {' '.join(args)} in {self.path} expected "
                f"rc={expect}, got {p.returncode}:\n{p.stdout}\n{p.stderr}")
        return p.stdout + p.stderr

    def run_sh(self, cmd: str, expect: int = 0) -> str:
        p = subprocess.run(cmd, check=False, shell=True, cwd=str(self.path),
                           capture_output=True, text=True)
        if p.returncode != expect:
            raise AssertionError(
                f"shell {cmd!r} in {self.path} expected rc={expect}, got "
                f"{p.returncode}:\n{p.stdout}\n{p.stderr}")
        return p.stdout + p.stderr

    def commit(self, msg: str) -> None:
        self.run_sh("git add -A")
        self.run_sh(f"git commit -q -m {msg!r}")

    def md_snapshot(self) -> dict[str, str]:
        """Every `*.md` in the clone (docs/**, WIP.md, CHANGELOG.md, …) by
        path-relative-to-clone → content: the byte-mergeable project data."""
        out = {}
        for f in sorted(self.path.rglob("*.md")):
            rel = f.relative_to(self.path).as_posix()
            if ".git" in rel.split("/"):
                continue
            out[rel] = f.read_text(encoding="utf-8")
        return out

    def ls_json(self) -> str:
        return self.run("ls", "--json")

    def item_ls_json(self) -> str:
        return self.run("item", "ls", "--json")

    def state(self) -> dict:
        """The three things the design doc §10 asserts are byte-identical on
        both sides after a merge: every md + both json listings."""
        return {"md": self.md_snapshot(), "ls": self.ls_json(),
                "items": self.item_ls_json()}


@dataclass
class TwoClones:
    bare: Path
    a: Clone
    b: Clone

    def push(self, c: Clone) -> None:
        _git("push", "origin", "HEAD", cwd=c.path)

    def pull(self, c: Clone) -> None:
        _git("pull", "origin", "HEAD", "--no-rebase", cwd=c.path)

    def rebase(self, c: Clone) -> None:
        _git("pull", "origin", "HEAD", "--rebase", cwd=c.path)

    def merge_conflict(self) -> bool:
        """True if either clone has a mid-merge conflict (unmerged paths)."""
        for c in (self.a, self.b):
            out = _git("diff", "--name-only", "--diff-filter=U", cwd=c.path)
            if out.strip():
                return True
            if (c.path / ".git" / "MERGE_HEAD").exists():
                return True
        return False


def build_two_clones(tmp_path: Path, base_setup=None,
                     install_hooks: bool = True) -> TwoClones:
    """Bare repo → `proj init` + commit the base → clone into A and B (each
    runs `proj hooks install`). `base_setup` optionally mutates one clone
    before the first push (e.g. add a task) so A and B start divergent."""
    proj = _find_proj()
    bare = tmp_path / "bare.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)

    base_dir = tmp_path / "base"
    _git("clone", "-q", str(bare), str(base_dir), cwd=tmp_path)
    _configure(base_dir)
    base = Clone(path=base_dir, proj=proj)
    base.run("init", "--profile", "default")
    if base_setup is not None:
        base_setup(base)
    # S.10: .proj/bugs.log + write.lock are excluded by the committed
    # .proj/.gitignore (scaffold) — no local exclude entry needed
    # hooks install on the base: commits .gitattributes into the project (C5)
    # so A/B inherit it tracked; their own install only writes local git
    # config + hooks (both inside .git)
    if install_hooks:
        base.run("hooks", "install")
        base.hooks = True
    base.commit("base project")
    _git("push", "-q", "origin", "HEAD", cwd=base_dir)

    a, b = tmp_path / "a", tmp_path / "b"
    _git("clone", "-q", str(bare), str(a), cwd=tmp_path)
    _git("clone", "-q", str(bare), str(b), cwd=tmp_path)
    clones = [Clone(path=a, proj=proj), Clone(path=b, proj=proj)]
    for c in clones:
        _configure(c.path)
        if not install_hooks:
            continue  # the no-hooks scenario: a clone without any hook
        try:
            c.run("hooks", "install")
            c.hooks = True
        except AssertionError:
            c.hooks = False  # E4 still in progress; scenarios don't need it
    return TwoClones(bare=bare, a=clones[0], b=clones[1])
