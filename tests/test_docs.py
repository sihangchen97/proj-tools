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
    for heading, block in sections.items():
        if heading == "所有 record 共用的命令":
            continue
        assert f"proj {heading} new" not in block
        assert f"proj {heading} ls" not in block
        assert f"proj {heading} show" not in block
        assert f"proj {heading} update" not in block
