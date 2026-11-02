"""A13 (§19): the `.proj/bugs.log` report mechanism (append-only, DB-free, auto)."""
from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from proj import bugs
from proj.cli import app
from proj.core.config import ProjConfig
from proj.engine import store as rec

runner = CliRunner()


def test_two_appends_give_exactly_two_blocks(proj):
    p = bugs.append_bug("first", "cli", cmd="proj a", root=proj.root)
    p2 = bugs.append_bug("second", "cli", cmd="proj b", root=proj.root)
    assert p == p2
    content = p.read_text(encoding="utf-8")
    assert content.count("=== ") == 2
    assert bugs.count(proj.root) == 2
    recs = bugs.list_bugs(proj.root)
    assert [r["source"] for r in recs] == ["cli", "cli"]


def test_multiline_and_indented_header(proj):
    bugs.append_bug("line1\n=== a line that looks like a header\nline3",
                    "cli", root=proj.root)
    content = (proj.root / bugs.BUGS_FILE).read_text(encoding="utf-8")
    # the body line is indented (two leading spaces) so it is not a real header
    assert "=== a line that looks like a header" not in content.split("=== ")[1]
    assert "  === a line that looks like a header" in content
    assert bugs.count(proj.root) == 1
    # and it parses back with the indentation stripped
    assert bugs.list_bugs(proj.root)[0]["body"] == \
        "line1\n=== a line that looks like a header\nline3"


def test_db_free_with_broken_database(proj, tmp_path):
    # B5: a corrupt DB file must never stop a report being written
    (proj.db_path).write_bytes(b"\x00\x01garbage")
    p = bugs.append_bug("still works", "cli", root=proj.root)
    assert p.is_file()
    assert "still works" in p.read_text(encoding="utf-8")


def test_auto_records_unexpected_but_not_normal(proj, monkeypatch):
    """B4: the `main()` entry point auto-records an unexpected exception (and
    only those) before exiting non-zero."""
    import sys

    from proj import cli

    monkeypatch.chdir(proj.root)

    def unexpected():
        raise RuntimeError("name 'spec_of' is not defined")

    monkeypatch.setattr(cli, "app", unexpected)
    monkeypatch.setattr(sys, "argv", ["proj", "info"])
    with pytest.raises(typer.Exit) as e:
        cli.main()
    assert e.value.exit_code == 1
    content = (proj.root / bugs.BUGS_FILE).read_text(encoding="utf-8")
    assert "auto" in content and "Traceback" in content
    assert "proj info" in content  # the command line
    assert bugs.count(proj.root) == 1

    # a normal error (ValueError) is NOT recorded
    def normal():
        raise ValueError("nope")

    monkeypatch.setattr(cli, "app", normal)
    with pytest.raises(typer.Exit):
        cli.main()
    assert bugs.count(proj.root) == 1  # unchanged


def test_auto_record_unit(monkeypatch):
    # the helper itself: source `auto`, command + traceback in the body
    import tempfile

    d = tempfile.mkdtemp()
    monkeypatch.chdir(d)
    try:
        raise NameError("spec_of is not defined")
    except NameError as exc:
        bugs.auto_record(exc, "auto", cmd="proj sync", root=d)
    rec = bugs.list_bugs(root=d)[0]
    assert rec["source"] == "auto" and rec["cmd"] == "proj sync"
    assert "Traceback" in rec["body"] and "NameError" in rec["body"]


def test_init_gitignore_idempotent(root):
    """S.10: .proj/.gitignore lists the personal/runtime files; repeating init
    leaves a user-edited .proj/.gitignore alone (no duplicate, no clobber), and
    the project-root .gitignore is no longer created by init."""
    from proj import scaffold
    scaffold.init(ProjConfig(root=root), ["default"])
    gi = (root / ".proj" / ".gitignore").read_text(encoding="utf-8")
    assert "bugs.log" in gi.split()
    # a user-edited .proj/.gitignore is left alone (add() is idempotent)
    (root / ".proj" / ".gitignore").write_text("my-local-thing\n")
    scaffold.init(ProjConfig(root=root), ["default"])
    assert (root / ".proj" / ".gitignore").read_text(
        encoding="utf-8").splitlines() == ["my-local-thing"]
    # the project-root .gitignore is no longer created by init
    assert not (root / ".gitignore").exists()


def test_info_shows_bug_reports(proj):
    assert rec.info(proj)["bug_reports"] is None
    assert "Bug reports" not in rec.info_text(rec.info(proj))
    bugs.append_bug("x", "cli", root=proj.root)
    d = rec.info(proj)
    assert d["bug_reports"] == 1
    assert "Bug reports: 1 (.proj/bugs.log)" in rec.info_text(d)


def test_cli_bug_and_ls(proj, monkeypatch):
    monkeypatch.chdir(proj.root)
    assert runner.invoke(app, ["bug", "something broke", "--cmd", "proj sync"]).exit_code == 0
    assert "something broke" in runner.invoke(app, ["bug", "ls"]).output
    out = runner.invoke(app, ["bug", "ls", "--json"]).output
    import json
    assert json.loads(out)[0]["source"] == "cli"


def test_mcp_bug_writes_to_file(proj):
    from proj import mcp
    out = mcp.proj_bug("an mcp bug", command="proj x", root=str(proj.root))
    assert out.startswith("recorded to")
    assert (proj.root / bugs.BUGS_FILE).is_file()
    assert "an mcp bug" in mcp.proj_bug_ls(root=str(proj.root))
