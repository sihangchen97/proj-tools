"""E4: `proj hooks install` — .gitattributes, the proj-render merge driver,
and the pre/post hooks (C5 of the git-collab design)."""
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from proj import hooks
from proj.cli import app

runner = CliRunner()


def _git(root, *args) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True, text=True)
    return p.stdout.strip()


@pytest.fixture
def git_project(proj, monkeypatch):
    """The tmp proj project turned into a git repo (hooks can be installed)."""
    _git(proj.root, "init", "-q")
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    return proj


def test_install_writes_gitattributes_and_driver(git_project):
    out = hooks.install(git_project)
    assert "hooks" in out
    attr = (git_project.root / ".gitattributes").read_text(encoding="utf-8")
    assert ".proj/events.jsonl  merge=union" in attr
    assert "docs/**/*.md        merge=proj-render" in attr
    assert "WIP.md              merge=proj-render" in attr
    assert "CHANGELOG.md        merge=proj-render" in attr
    assert _git(git_project.root, "config", "merge.proj-render.name") == \
        hooks.MERGE_DRIVER_NAME
    assert _git(git_project.root, "config", "merge.proj-render.driver") == "true"


def test_install_writes_all_four_hooks(git_project):
    hooks.install(git_project)
    hooks_dir = git_project.root / ".git" / "hooks"
    for name, cmd in (("pre-commit", "check"),
                      ("post-merge", "sync --quiet"),
                      ("post-checkout", "sync --quiet"),
                      ("post-rewrite", "sync --quiet")):
        body = (hooks_dir / name).read_text(encoding="utf-8")
        assert "proj hooks install" in body, name
        assert cmd in body, name
        # the embedded proj path is absolute (a bare `proj` on PATH could be a
        # different program) and exists
        m = re.search(r'exec "([^"]+)" ', body)
        assert m and m.group(1).startswith("/") and Path(m.group(1)).is_file(), \
            f"{name} must embed an absolute, existing proj path"
        assert (hooks_dir / name).stat().st_mode & 0o111, f"{name} not executable"
    # post-checkout skips a plain file checkout (the guard sits BEFORE the exec)
    co = (hooks_dir / "post-checkout").read_text(encoding="utf-8")
    assert co.index('exit 0') < co.index("exec ")


def test_install_is_idempotent(git_project):
    hooks.install(git_project)
    first = (git_project.root / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    hooks.install(git_project)  # second run: everything ours again, no error
    got = (git_project.root / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert got == first


def test_install_refuses_foreign_hook(git_project):
    hooks.install(git_project)
    (git_project.root / ".git" / "hooks" / "post-merge").write_text(
        "#!/bin/sh\n# someone else's hook\nexit 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a proj hook"):
        hooks.install(git_project)


def test_install_requires_git(proj):
    with pytest.raises(ValueError, match="git init"):
        hooks.install(proj)


def test_cli_hooks_install(git_project, monkeypatch):
    monkeypatch.chdir(git_project.root)
    r = runner.invoke(app, ["hooks", "install"])
    assert r.exit_code == 0, r.output
    assert "proj check" in r.output
    assert (git_project.root / ".gitattributes").is_file()
