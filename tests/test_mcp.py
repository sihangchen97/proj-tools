"""MCP tool bodies: direct calls in a tmp proj sandbox (no stdio protocol)."""
from __future__ import annotations

from proj import mcp


def test_mcp_init_new_status_check_chain(proj):
    root = str(proj.root)
    out = mcp.proj_init(root=root)
    assert "error" not in out

    out = mcp.proj_new("plan", "Ship v1", root=root)
    assert out.startswith("Created") and "error" not in out
    plan_id = out.split("docs/plans/")[1][:8]  # PLN-0001

    out = mcp.proj_new("task", "Baseline", parent=plan_id, root=root)
    assert "Created" in out and "error" not in out
    task_id = out.split("tasks/")[1][:8]  # docs/tasks/T-000001-...

    out = mcp.proj_show("task", task_id, root=root)
    assert f"id: {task_id}" in out and f"parent: {plan_id}" in out

    out = mcp.proj_verb("task", "start", task_id, root=root)
    assert f"task {task_id}: status → In Progress" in out

    out = mcp.proj_verb("plan", "activate", plan_id, root=root)
    assert "Active" in out and "error" not in out

    out = mcp.proj_check(root=root)
    assert out == "OK"

    out = mcp.proj_info(root=root, as_json=True)
    assert '"plan"' in out


def test_mcp_update_record(proj):
    root = str(proj.root)
    out = mcp.proj_update_record("decision", "DEC-0001",
                                title="Renamed", summary="one line",
                                message="Changed: refined", root=root)
    assert "title → Renamed" in out and "error" not in out
    assert "error" in mcp.proj_update_record("decision", "DEC-0001", root=root)  # nothing
    assert "error" in mcp.proj_update_record("decision", "DEC-9999", title="x", root=root)
    # verb also updates after the step (fresh decision: Draft → Accepted + -c)
    out = mcp.proj_new("decision", "Fresh", root=root)
    did = out.rsplit("/", 1)[1].rsplit("-", 1)[0]  # DEC-0002
    assert did.startswith("DEC-")
    out = mcp.proj_verb("decision", "accept", did, message="Added: ok", root=root)
    assert "Accepted" in out and "error" not in out
    # a comment renders as the status-at-that-moment emoji + the comment text
    assert "[Added] ✅ ok" in mcp.proj_show("decision", did, history=True, root=root)


def test_mcp_record_type_code_and_case(proj):
    root = str(proj.root)
    # code (case-insensitive) resolves to the canonical record_type
    out = mcp.proj_new("DEC", "Via code", root=root)
    assert "Created docs/decisions/DEC-" in out and "error" not in out
    out = mcp.proj_new("T", "Via code", root=root)
    assert "Created docs/tasks/T-" in out and "error" not in out
    assert mcp.proj_list("debt", root=root)
    assert "DEC-" in mcp.proj_show("DEC", "DEC-0001", root=root)


def test_mcp_error_paths(proj):
    root = str(proj.root)
    assert "unknown record_type" in mcp.proj_new("nope", "X", root=root)
    # parent must exist in some record_type
    assert "not found in any record_type" in mcp.proj_new("task", "X", parent="ZZZ", root=root)
    # verb must be one the template declares
    assert "unknown verb" in mcp.proj_verb("task", "explode", "A", root=root)
    # unknown record_type for a verb
    assert "unknown record_type" in mcp.proj_verb("nope", "done", "1", root=root)
    # show: nonexistent record
    assert "not found" in mcp.proj_show("decision", "9999", root=root)
    # supersede: same-type required (decision cannot supersede a task)
    mcp.proj_new("task", "X", root=root)
    assert "same type required" in mcp.proj_new("decision", "X2", supersede="T-000001", root=root)

def test_parent_rejected_for_non_task(proj):
    """A3: proj_new(parent=...) on a non-task record_type errors (was silently
    ignored before); task still accepts it."""
    out = mcp.proj_new("decision", "X", parent="PLN-0001", root=str(proj.root))
    assert "only supported by task" in out

def test_safe_returns_clean_error_for_sqlite_and_projerror(proj):
    """D4: _safe returns 'error:' text for sqlite/ProjError instead of a traceback."""
    import sqlite3

    from proj.mcp import ProjError, _safe

    def boom_sqlite():
        raise sqlite3.OperationalError("database is locked")

    def boom_proj():
        raise ProjError("gate failed")

    assert _safe(boom_sqlite) == "error: database is locked"
    assert _safe(boom_proj) == "error: gate failed"
