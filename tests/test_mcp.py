"""MCP tool bodies: direct calls in a tmp proj sandbox (no stdio protocol)."""
from __future__ import annotations

from proj import mcp


def test_mcp_init_new_status_check_chain(proj):
    root = str(proj.root)
    out = mcp.proj_init(root=root)
    assert "error" not in out

    out = mcp.proj_new("plan", "Ship v1", root=root)
    assert out.startswith("Created") and "error" not in out
    plan_id = out.split("docs/plans/")[1].split("-")[0]

    out = mcp.proj_new("task", "Baseline", parent=plan_id, root=root)
    assert "Created" in out and "error" not in out
    task_id = out.split("tasks/")[1][0]

    out = mcp.proj_show("task", task_id, root=root)
    assert f"id: {task_id}" in out and f"parent: {plan_id}" in out

    out = mcp.proj_status("task", task_id, "In Progress", root=root)
    assert f"task {task_id}: status → In Progress" in out

    out = mcp.proj_plan_verb("activate", plan_id, root=root)
    assert "Active" in out and "error" not in out

    out = mcp.proj_check(root=root)
    assert out == "OK"

    out = mcp.proj_info(root=root, as_json=True)
    assert '"plan"' in out


def test_mcp_error_paths(proj):
    root = str(proj.root)
    assert "unknown record_type" in mcp.proj_new("nope", "X", root=root)
    # parent must exist in some record_type
    assert "parent 'ZZZ' not found" in mcp.proj_new("task", "X", parent="ZZZ", root=root)
    # status must be in the template's allowed set
    assert "error" in mcp.proj_status("task", "A", "Bogus", root=root)
    # plan verb must be a known verb
    assert "plan verb must be one of" in mcp.proj_plan_verb("explode", "1", root=root)
    # show: nonexistent record
    assert "not found" in mcp.proj_show("decision", "9999", root=root)
    # bad --field parsing
    assert "field expects name=value" in mcp.proj_new("debt", "X", field="noequals", root=root)
