"""`--root` default = cwd (no walking up) + bare `proj` project status."""
from __future__ import annotations

import pytest
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
    assert (empty / ".proj" / "cache.sqlite").is_file()  # S.10
    assert (empty / ".proj" / "lock.yaml").is_file()


def test_root_points_at_project(proj, tmp_path, monkeypatch):
    empty = tmp_path / "elsewhere"  # cwd is NOT a project
    empty.mkdir()
    monkeypatch.chdir(empty)
    r = runner.invoke(app, ["--root", str(proj.root), "check"])
    assert r.exit_code == 0, r.output
    assert "OK" in r.output


def test_cheatsheet_options_all_exist(proj, monkeypatch):
    """A8 (§17.1): every option the `proj info` cheatsheet shows must really
    exist on the command it documents (the RT sub-commands + lifecycle)."""
    import re

    from typer.main import get_command

    from proj.cli import _build_record_group
    from proj.engine.store import CHEATSHEET

    def flags(cmd) -> set[str]:
        out = {o for p in getattr(cmd, "params", []) for o in p.opts}
        for sub in getattr(cmd, "commands", {}).values():
            out |= flags(sub)
        return out

    monkeypatch.chdir(proj.root)
    allowed = flags(_build_record_group("task")) | flags(get_command(app).commands["info"])
    long_opts = set(re.findall(r"--[a-z][a-z0-9-]*", CHEATSHEET))
    short_opts = {"-" + s for s in re.findall(r"(?<![-\w])-([a-z])", CHEATSHEET)}
    missing = (long_opts | short_opts) - allowed
    assert not missing, f"cheatsheet options that don't exist: {sorted(missing)}"


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
    cli_md = (tmp_path / ".proj/CLI.md").read_text()
    mcp_md = (tmp_path / ".proj/MCP.md").read_text()
    assert "proj task new" in cli_md  # dynamic record_type groups, generated
    assert "proj decision accept" in cli_md  # template verb, generated
    assert "proj_verb" in mcp_md  # generated from the real MCP server
    assert "当前目录" in cli_md  # the --root default, in the generated intro

def test_parent_rejected_for_non_task(proj, monkeypatch):
    """A3: --parent only exists for task; `decision new --parent` is an error,
    not a silent no-op (the option used to be registered on every type)."""
    monkeypatch.chdir(proj.root)
    r = runner.invoke(app, ["decision", "new", "X", "--parent", "T-000001", "-y"])
    assert r.exit_code == 1, r.output
    assert "only supported by task" in r.output
    # a plain new still works (commit is a generic records column — kept)
    r = runner.invoke(app, ["decision", "new", "Ok", "-y"])
    assert r.exit_code == 0, r.output

def test_s_is_summary_on_new(proj, monkeypatch):
    """C1: -s means --summary on `new` (was --supersede); supersede is long-only.
    `-s <id>` must NOT create a supersede edge — it becomes the summary."""
    monkeypatch.chdir(proj.root)
    r = runner.invoke(app, ["decision", "new", "S", "-s", "DEC-0001", "-y"])
    assert r.exit_code == 0, r.output
    # the "DEC-0001" text went to the summary, not the supersede relation
    out = runner.invoke(app, ["decision", "show", "DEC-0002"]).output
    assert "DEC-0001" in out and "supersedes" not in out
    # --supersede long-only still creates the edge
    r = runner.invoke(app, ["decision", "new", "S2", "--supersede", "DEC-0001", "-y"])
    assert r.exit_code == 0, r.output

@pytest.mark.render_drift
def test_unsynced_edit_survives_an_unrelated_write(proj, monkeypatch):
    """A1 (the original bug): hand-editing a record's md and running ANOTHER
    write command (task new) used to silently clobber the edit — every write
    rendered ALL md from the DB. Now the edit is auto-synced and survives."""
    monkeypatch.chdir(proj.root)
    out = runner.invoke(app, ["decision", "new", "D1", "-y"]).output
    rel = out.split("docs/decisions/")[1].strip()        # DEC-0002-d1.md
    rid = "-".join(rel.split("-")[:2])                   # the record id (DEC-0002)
    target = proj.root / "docs/decisions" / rel
    target.write_text(target.read_text().replace(
        "## Context", "## Context\n\nhand-written line kept", 1))
    # an UNRELATED write command (task new) must not lose the hand edit
    r = runner.invoke(app, ["task", "new", "Unrelated", "-y"])
    assert r.exit_code == 0, r.output
    assert "hand-written line kept" in target.read_text()
    # ...and it was ingested into the DB (A1 auto-sync, not clobbered)
    import json

    from proj.engine import store as rec
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    row = db.get(spec, rid)
    db.close()
    assert "hand-written line kept" in json.loads(row["sections"]).get("context", "")

def test_cross_type_show_and_ls(proj, monkeypatch):
    """C5: the id carries the type — `proj show ID` / `proj ls` work without
    a per-type subcommand."""
    monkeypatch.chdir(proj.root)
    rid = "DEC-" + runner.invoke(app, ["decision", "new", "Cross", "-y"])\
        .output.split("docs/decisions/")[1].split("-")[1]
    tid = runner.invoke(app, ["task", "new", "Tasky", "-y"])\
        .output.split("docs/tasks/")[1].split("-")[0]
    out = runner.invoke(app, ["show", rid]).output
    assert f"id: {rid}" in out and "Cross" in out
    out = runner.invoke(app, ["ls", "--type", "task"]).output
    assert tid in out
    out = runner.invoke(app, ["ls"]).output          # every record_type
    assert rid in out and tid in out
    # unknown id / unknown type are clean errors
    assert runner.invoke(app, ["show", "DEC-9999"]).exit_code != 0
    assert runner.invoke(app, ["ls", "--type", "bogus"]).exit_code != 0


def test_list_is_a_hidden_alias_for_ls(proj, monkeypatch):
    """C4: `list` still works (hidden alias) but only `ls` is advertised."""
    monkeypatch.chdir(proj.root)
    help = runner.invoke(app, ["decision", "--help"]).output
    assert "ls" in help
    assert runner.invoke(app, ["decision", "ls"]).exit_code == 0
    assert runner.invoke(app, ["decision", "list"]).exit_code == 0


def test_json_outputs_parse(proj):
    """R2/C10: every --json output is ONLY JSON (it used to crash — the option
    shadowed the json module — or print the text table before the JSON), and
    per-type `ls`/`show` take --json too. `item ls --json` gives objects."""
    import json

    root = ["--root", str(proj.root)]
    assert runner.invoke(app, [*root, "task", "new", "T", "-y"]).exit_code == 0
    assert runner.invoke(app, [*root, "item", "add", "T-000001", "a"]).exit_code == 0
    for cmd, kind in ((["ls"], list), (["task", "ls"], list), (["task", "list"], list),
                      (["show", "T-000001"], dict), (["task", "show", "T-000001"], dict),
                      (["item", "ls"], list)):
        r = runner.invoke(app, [*root, *cmd, "--json"])
        assert r.exit_code == 0, (cmd, r.output)
        assert isinstance(json.loads(r.output), kind), cmd
    rows = json.loads(runner.invoke(app, [*root, "item", "ls", "--json"]).output)
    assert rows[0]["ref"] == "T-000001#001" and rows[0]["closed"] is False
