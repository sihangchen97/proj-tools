"""tests/render_verify.verify: the disk must equal a full in-memory render."""
from __future__ import annotations

import pytest
import render_verify


def test_clean_project_verifies_empty(proj):
    assert render_verify.verify(proj) == []


@pytest.mark.render_drift
def test_tampered_record_is_detected(proj):
    p = proj.root / "docs" / "decisions" / "DEC-0001-record-architecture-decisions.md"
    assert p.exists()
    assert render_verify.verify(proj) == []
    p.write_text(p.read_text(encoding="utf-8") + "\nhand-tamper\n", encoding="utf-8")
    diffs = render_verify.verify(proj)
    assert len(diffs) == 1
    assert diffs[0][0] == "docs/decisions/DEC-0001-record-architecture-decisions.md"
    assert diffs[0][1] == "differs"
