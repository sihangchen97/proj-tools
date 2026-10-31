"""`--root` default = cwd (no walking up) + bare `proj` project status."""
from __future__ import annotations

from typer.testing import CliRunner

from proj.cli import app

runner = CliRunner()


def test_non_project_cwd_fails_with_init_hint(tmp_path, monkeypatch):
    """From a non-project cwd, commands fail with the 'only init works' message;
    init itself still works (non-strict root = cwd)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    r = runner.invoke(app, ["decision", "new", "x"])  # dynamic record_type cmd
    assert r.exit_code == 1
    assert "no proj project in" in r.output
    assert "proj init" in r.output
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    assert (empty / ".proj.sqlite").is_file()


def test_root_points_at_project(proj, tmp_path, monkeypatch):
    empty = tmp_path / "elsewhere"  # cwd is NOT a project
    empty.mkdir()
    monkeypatch.chdir(empty)
    r = runner.invoke(app, ["--root", str(proj.root), "check"])
    assert r.exit_code == 0, r.output
    assert "OK" in r.output


def test_record_type_code_and_case(proj, monkeypatch):
    """`proj <record_type>` accepts the code (DEC/T/…) and any casing."""
    monkeypatch.chdir(proj.root)
    assert runner.invoke(app, ["DEC", "list"]).exit_code == 0
    assert "DEC-0001" in runner.invoke(app, ["Decision", "list"]).output
    assert runner.invoke(app, ["T", "list"]).exit_code == 0
    assert runner.invoke(app, ["ZZZ", "list"]).exit_code != 0
    # subcommands/verbs are case-insensitive too (token_normalize_func=lower)
    out = runner.invoke(app, ["DEC", "ACCEPT", "DEC-0001"])
    assert out.exit_code == 0, out.output
    assert "status → Accepted" in out.output


def test_bare_proj_shows_status(proj, monkeypatch):
    monkeypatch.chdir(proj.root)
    r = runner.invoke(app, [])
    assert r.exit_code == 0, r.output
    assert "Project:" in r.output


def test_bare_proj_in_empty_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, [])
    assert r.exit_code == 1
    assert "no proj project in" in r.output
    assert "proj init" in r.output


def test_docs_command_generates_both_files(tmp_path, monkeypatch):
    # no full project needed: `docs` writes with a non-strict root (cwd)
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["docs"])
    assert r.exit_code == 0, r.output
    cli_md = (tmp_path / "docs/CLI.md").read_text()
    mcp_md = (tmp_path / "docs/MCP.md").read_text()
    assert "proj task new" in cli_md  # dynamic record_type groups, generated
    assert "proj decision accept" in cli_md  # template verb, generated
    assert "proj_verb" in mcp_md  # generated from the real MCP server
    assert "当前目录" in cli_md  # the --root default, in the generated intro
