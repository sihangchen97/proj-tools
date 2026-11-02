"""docs.py: the generated reference never drifts from the real CLI surface."""
from __future__ import annotations

from proj import docs


def test_positional_args_render_once(proj):
    """C7/D5 regression: a positional `ID` param renders as `<ID>`, not the
    leaked `ID <ID>` (typer's TyperArgument opts carry no '-' prefix)."""
    sections = docs._cli_sections()
    top = sections[0][1]
    assert "proj show <ID>" in top
    assert "proj open <ID>" in top
    assert "ID <ID>" not in top


def test_command_surface_is_current(proj):
    """The reference reflects the C2-C5 surface: no global update, no per-type
    render, cross-type show/ls/open, and the hidden `list` alias stays out."""
    sections = docs._cli_sections()
    top = sections[0][1]
    assert "proj update" not in top
    assert "proj show <ID>" in top and "proj ls" in top and "proj open <ID>" in top
    for heading, block in sections:
        if heading == "所有 record 共用的命令":
            assert "proj <rt> list" not in block
            assert "proj <rt> render" not in block
            assert "proj <rt> ls" in block
        if heading in ("task", "decision"):
            assert f"proj {heading} list" not in block
            assert f"proj {heading} render" not in block


def test_type_sections_only_type_specific_verbs(proj):
    """Shared commands (new/ls/show/update) appear once (the common block) and
    are not repeated under each record_type heading."""
    sections = dict(docs._cli_sections())
    common = sections["所有 record 共用的命令"]
    for cmd in ("new", "ls", "show", "update"):
        assert f"proj <rt> {cmd}" in common
    # only record_type sections are checked (item/changelog/hooks are static groups)
    from proj.engine import store as rec
    for heading in rec.load_registry(None):
        block = sections[heading]
        for cmd in ("new", "ls", "show", "update"):
            assert f"proj {heading} {cmd}" not in block

# ---------------------------------------------------------------------------
# §28.1 23:10: the generated reference must equal the committed files (docs/
# AND the templates/ seed copies) — nobody may hand-edit the four generated
# files or change the CLI/MCP surface without re-running `pixi run docs`.


def _repo():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


def test_docs_match_generated(proj):
    """docs/CLI.md == templates/PROJ_CLI.md == what docs.py generates now."""
    repo = _repo()
    cli = docs.cli_reference()
    mcp = docs.mcp_reference()
    for pair in ((cli, repo / "docs" / "CLI.md", repo / "templates" / "PROJ_CLI.md"),
                 (mcp, repo / "docs" / "MCP.md", repo / "templates" / "PROJ_MCP.md")):
        text, *paths = pair
        for p in paths:
            got = p.read_text(encoding="utf-8")
            assert got == text, (f"{p} has drifted from the generated reference — "
                                 "edit src/proj/docs.py (or cli.py / mcp.py) then run "
                                 "`pixi run docs`")
