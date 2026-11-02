"""C5 finishing: refuse writes mid-merge/rebase, conflict-marker detection in
sync, and `sync --rerender` (PLAN section 28.1, 22:55 table stage 2; spec
plans/hard-parts-impl.md C5).

The single choke point is Db.begin (every write command funnels through it,
CLI + MCP); the predicate is hooks.git_in_progress. sync detects a git conflict
marker the proj-render driver would have removed and points at the fix;
`proj sync --rerender` overwrites every md with the rendered state (force).
"""
import subprocess

import pytest
from typer.testing import CliRunner

from proj.cli import app
from proj.core.config import ProjConfig
from proj.engine import store as rec
from proj.hooks import git_in_progress
from proj.record_types import task as taskmod

runner = CliRunner()


def _git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], check=check,
                          capture_output=True, text=True)


@pytest.fixture
def git_project(proj, monkeypatch):
    """The tmp proj project turned into a git repo."""
    _git(proj.root, "init", "-q")
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    return proj


# ---- git_in_progress: the refusal predicate --------------------------------

def test_git_in_progress_none_when_not_git(proj):
    # a plain project has no .git — never refuses
    assert git_in_progress(ProjConfig(root=proj.root)) is None


def test_git_in_progress_detects_merge_head(git_project):
    marker = git_project.root / ".git" / "MERGE_HEAD"
    marker.write_text("ref: refs/heads/feat\n")
    try:
        assert "merge" in git_in_progress(ProjConfig(root=git_project.root))
    finally:
        marker.unlink()


def test_git_in_progress_detects_rebase_head(git_project):
    marker = git_project.root / ".git" / "REBASE_HEAD"
    marker.write_text("ref: refs/heads/feat\n")
    try:
        assert "rebase" in git_in_progress(ProjConfig(root=git_project.root))
    finally:
        marker.unlink()


def test_git_in_progress_none_when_clean(git_project):
    _git(git_project.root, "add", "-A")
    _git(git_project.root, "commit", "-q", "-m", "base")
    assert git_in_progress(ProjConfig(root=git_project.root)) is None


# ---- C7: a write command is refused mid-merge (real git conflict) ---------

@pytest.mark.render_drift
def test_write_refused_while_merge_in_progress(git_project):
    root = git_project.root
    taskmod.new(git_project, "base")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    # two branches change the SAME lines of the task md to different values
    _git(root, "checkout", "-q", "-b", "feat")
    p = next(root.glob("docs/tasks/T-000001-*.md"))
    p.write_text(p.read_text(encoding="utf-8").replace("Steps", "STEPS-FEAT"),
                 encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feat")
    _git(root, "checkout", "-q", branch)
    p = next(root.glob("docs/tasks/T-000001-*.md"))
    p.write_text(p.read_text(encoding="utf-8").replace("Steps", "STEPS-MAIN"),
                 encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "main")
    _git(root, "merge", "feat", check=False)  # conflict on the same lines
    assert (root / ".git" / "MERGE_HEAD").exists()
    try:
        with pytest.raises(ValueError, match="merge"):
            taskmod.new(git_project, "refused")
    finally:
        _git(root, "merge", "--abort", check=False)
    assert not (root / ".git" / "MERGE_HEAD").exists()
    taskmod.new(git_project, "ok")  # unblocked once the merge is gone


# ---- conflict marker: sync points at the fix -------------------------------

@pytest.mark.render_drift
def test_sync_reports_conflict_marker(proj):
    taskmod.new(proj, "T1")
    p = next(proj.root.glob("docs/tasks/T-000001-*.md"))
    p.write_text(p.read_text(encoding="utf-8") +
                 "\n<<<<<<< .mine\nA\n=======\nB\n>>>>>>> .their\n",
                 encoding="utf-8")
    problems, _ = rec.sync(proj)
    assert any("conflict marker" in x and "hooks install" in x and "--rerender" in x
               for x in problems), problems


# ---- sync --rerender: overwrite every md with the rendered state -----------

def test_render_all_force_overwrites(proj):
    taskmod.new(proj, "T1")
    p = next(proj.root.glob("docs/tasks/T-000001-*.md"))
    p.write_text(p.read_text(encoding="utf-8") +
                 "\n<<<<<<< .mine\nA\n=======\nB\n>>>>>>> .their\n",
                 encoding="utf-8")
    written = rec.render_all(proj, force=True)
    assert p in written
    assert "<<<<<<<" not in p.read_text(encoding="utf-8")


def test_cli_sync_rerender(proj, monkeypatch):
    monkeypatch.chdir(proj.root)
    taskmod.new(proj, "T1")
    p = next(proj.root.glob("docs/tasks/T-000001-*.md"))
    p.write_text(p.read_text(encoding="utf-8") +
                 "\n<<<<<<< .mine\nA\n=======\nB\n>>>>>>> .their\n",
                 encoding="utf-8")
    r = runner.invoke(app, ["sync", "--rerender"])
    assert r.exit_code == 0, r.output
    assert "re-rendered" in r.output
    assert "<<<<<<<" not in p.read_text(encoding="utf-8")
