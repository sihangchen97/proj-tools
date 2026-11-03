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

    # K2: parent is task→task only — a task attaches to a plan via the plan's
    # Work checklist reference instead. A bare task (no parent) is fine.
    out = mcp.proj_new("task", "Baseline", root=root)
    assert "Created" in out and "error" not in out
    task_id = out.split("tasks/")[1][:8]  # docs/tasks/T-000001-...
    # the plan references the task from its Work checklist (→ T-…)
    out = mcp.proj_item_add(ref=plan_id, text="run the baseline", task=task_id,
                            root=root)
    assert "error" not in out and task_id in out

    # D2: the id carries the type — record_type is optional
    out = mcp.proj_show(task_id, root=root)
    assert f"id: {task_id}" in out
    # as_json (C10/D5): the same fields as a JSON object; picked_by carries the
    # reverse ref (K4) — the plan that references this task
    out = mcp.proj_show(task_id, as_json=True, root=root)
    assert f'"id": "{task_id}"' in out and f'"{plan_id}"' in out  # picked_by

    out = mcp.proj_verb(task_id, "start", root=root)
    assert f"task {task_id}: status → In Progress" in out

    out = mcp.proj_verb(plan_id, "activate", root=root)
    assert "Active" in out and "error" not in out

    out = mcp.proj_check(root=root)
    assert out == "OK"

    out = mcp.proj_info(root=root, as_json=True)
    assert '"plan"' in out

    # D1: the formerly missing tools exist and work
    assert "Rendered" in mcp.proj_render(plan_id, root=root)
    assert "OK" in mcp.proj_adopt(root=root)
    mcp.proj_changelog_add("Added: ship", root=root)
    assert "Released v" in mcp.proj_changelog_version(bump="minor", root=root)
    assert "ship" in mcp.proj_changelog_release_notes("v0.1.0", root=root)
    assert "v0.1.0" in mcp.proj_changelog_list(root=root)


def test_mcp_update_record(proj):
    root = str(proj.root)
    # D2: proj_update_record is proj_update now (id first, type optional)
    out = mcp.proj_update("DEC-0001", title="Renamed", summary="one line",
                          message="Changed: refined", root=root)
    assert "title → Renamed" in out and "error" not in out
    assert "error" in mcp.proj_update("DEC-0001", root=root)  # nothing
    assert "error" in mcp.proj_update("DEC-9999", title="x", root=root)
    # verb also updates after the step (fresh decision: Draft → Accepted + -c)
    out = mcp.proj_new("decision", "Fresh", root=root)
    did = out.rsplit("/", 1)[1].rsplit("-", 1)[0]  # DEC-0002
    assert did.startswith("DEC-")
    out = mcp.proj_verb(did, "accept", message="Added: ok", root=root)
    assert "Accepted" in out and "error" not in out
    # a comment renders as the status-at-that-moment emoji + the comment text
    assert "[Added] ✅ ok" in mcp.proj_show(did, history=True, root=root)


def test_mcp_record_type_passthrough(proj):
    """The record_type argument is passed through verbatim (case-insensitive
    code resolution itself is covered by test_cli_root)."""
    root = str(proj.root)
    assert mcp.proj_ls(record_type="debt", root=root)
    # an explicit record_type must MATCH the id's type (D2)
    assert "DEC-" in mcp.proj_show("DEC-0001", record_type="DEC", root=root)
    assert "error" in mcp.proj_show("DEC-0001", record_type="task", root=root)


def test_mcp_error_paths(proj):
    root = str(proj.root)
    assert "unknown record_type" in mcp.proj_new("nope", "X", root=root)
    # parent must exist in some record_type (B1: this refused new leaves no orphan
    # row — the task below is the explicit one the verb/supersede checks need)
    assert "not found (types:" in mcp.proj_new("task", "X", parent="ZZZ", root=root)
    mcp.proj_new("task", "Real task", root=root)  # T-000001
    # verb must be one the template declares
    assert "unknown verb" in mcp.proj_verb("T-000001", "explode", root=root)
    # unknown record_type for a verb (when given explicitly)
    assert "unknown record_type" in mcp.proj_verb("1", "done", record_type="nope", root=root)
    # show: nonexistent record
    assert "not found" in mcp.proj_show("9999", root=root)
    # supersede: same-type required (decision cannot supersede a task)
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


def test_tool_dispatch_reaches_each_own_function(proj):
    """§21.2: every registered tool is invoked through the REAL MCP dispatch
    (`mcp.call_tool`), not the module-level functions directly — a
    registration-closure bug (every wrapper delegating to the LAST registered
    function) only shows up here. Each call asserts a marker unique to its
    own function, and that no call fell into the auto-record fallback."""
    import asyncio

    from proj import mcp as m

    root = str(proj.root)
    m.proj_init(root=root)  # build the project via direct calls (not under test)
    m.proj_new("plan", "Ship v1", root=root)
    # K2: no plan→task parent; the task is bare and referenced from the plan's
    # Work checklist instead (see the proj_item_add case below)
    m.proj_new("task", "Baseline", root=root)

    def call(name: str, args: dict) -> str:
        res = asyncio.run(m.mcp.call_tool(name, args))
        content = res.content if hasattr(res, "content") else res
        return "".join(getattr(x, "text", str(x)) for x in content)

    m.proj_changelog_add(entry="Added: ship", root=root)
    m.proj_wip_add(ids=["PLN-0001"], root=root)
    m.proj_item_add(ref="PLN-0001", text="marker-item", root=root)

    # (tool, args, marker unique to ITS function)
    cases = [
        ("proj_init", {"root": root}, "Nothing to create"),
        ("proj_new", {"record_type": "zzz", "title": "x", "root": root},
         "unknown record_type"),
        ("proj_sync", {"root": root}, "synced and re-rendered"),
        ("proj_adopt", {"root": root}, "adopted and re-rendered"),
        ("proj_render", {"root": root}, "Rendered"),
        ("proj_check", {"root": root}, "OK"),
        ("proj_ls", {"root": root}, "T-000001"),
        ("proj_show", {"id": "PLN-0001", "root": root}, "id: PLN-0001"),
        ("proj_update", {"id": "T-000001", "summary": "s", "root": root},
         "T-000001"),
        ("proj_verb", {"id": "PLN-0001", "verb": "nope", "root": root},
         "unknown verb"),
        ("proj_info", {"root": root}, "Project:"),
        ("proj_changelog_add", {"entry": "Added: again", "root": root},
         "Added entry"),
        ("proj_changelog_version", {"bump": "patch", "root": root},
         "Released v"),
        ("proj_changelog_release_notes", {"version": "v9.9.9", "root": root},
         "no changelog entries"),
        ("proj_changelog_list", {"root": root}, "v0.0."),
        ("proj_task_move", {"id": "T-000001", "parent": "PLN-0001", "root": root},
         "parent"),
        ("proj_item_ls", {"ref": "PLN-0001", "root": root}, "marker-item"),
        ("proj_item_add", {"ref": "T-000001", "text": "t-item", "root": root},
         "#001"),
        ("proj_item_update", {"item": "PLN-0001#001", "state": "done", "root": root},
         "done"),
        ("proj_item_rm", {"item": "T-000001#001", "root": root}, ""),
        ("proj_wip_add", {"ids": ["T-000001"], "root": root}, "T-000001"),
        ("proj_wip_ls", {"root": root}, "PLN-0001"),
        ("proj_wip_return", {"ids": ["PLN-0001"], "root": root},
         "returned PLN-0001"),
        ("proj_bug", {"text": "marker-bug-text", "root": root}, "recorded to"),
        ("proj_bug_ls", {"root": root}, "marker-bug-text"),
    ]
    for name, args, marker in cases:
        out = call(name, args)
        assert "unexpected keyword argument" not in out, (name, out)
        assert "\nrecorded to " not in out, (name, out)  # B4 auto-record fallback
        if marker:
            assert marker in out, f"{name}: marker {marker!r} not in {out[:200]!r}"


def test_tools_registered_once_sync(proj):
    """D1/D3: every CLI command has a tool, registered ONCE (no async
    duplicate), so the tool set is enumerable from the server itself."""
    import asyncio

    names = {t.name for t in asyncio.run(mcp.mcp.list_tools())}
    for expected in ("proj_adopt", "proj_render", "proj_ls",
                     "proj_update", "proj_changelog_version",
                     "proj_changelog_release_notes", "proj_changelog_list",
                     "proj_task_move", "proj_item_update"):  # R8: merged item tools
        assert expected in names
    # no leftover old names
    assert "proj_update_record" not in names
    assert "proj_open" not in names
    assert "proj_list" not in names
    # the bodies are the registered tools themselves (single definition, D3)
    from proj.mcp import proj_show as show_body
    assert show_body.__name__ == "proj_show"


def test_cli_command_coverage(proj):
    """D1: every CLI command has an MCP tool (the coverage gate, §6.2 #4).
    Exceptions are installation/dev-only commands listed below. record_types'
    shared verbs (new/ls/show/update/verb) map to the generic tools; a
    type-specific one (task move) maps to proj_<type>_<verb>; item group
    expands to proj_item_group_<verb>."""
    import asyncio

    import typer.main

    from proj.cli import app

    tools = {t.name for t in asyncio.run(mcp.mcp.list_tools())}

    # explicit exceptions: install/dev-only (no MCP tool by design)
    # `open`: CLI-only (returns the md path for an editor/agent to read — an MCP
    # client already has file access, so there is no MCP equivalent; A5/§9.2).
    # `merge resolve` (B-4): a git-index op on a stuck merge (ls-files /
    # git rm / git commit) — like `hooks install`, no MCP equivalent
    # (MCP tools drive the DB/records; this one drives git itself).
    EXCEPTIONS = {"docs", "open", "hooks install", "init --zoo", "merge resolve"}
    # A13 (§19.3): `bug` is one capability → proj_bug (its `ls` shares it)

    def is_group(c):
        return hasattr(c, "commands") and bool(c.commands)

    def expected(path):
        parts = path.split()
        if path in EXCEPTIONS:
            return set()
        if len(parts) == 1:
            return {"proj_bug"} if parts[0] == "bug" else {f"proj_{parts[0]}"}
        # R8: the item MCP tools are merged (4 tools cover every item command —
        # capability, not one tool per command): ls→ls; add / group add→add
        # (new_group); set/check/uncheck/edit/mv / group rename / group mv→
        # update; rm / group rm→rm
        if parts[0] == "item":
            cmd = parts[1] + (f" group {parts[2]}" if parts[1] == "group" else "")
            if cmd in ("ls",):
                return {"proj_item_ls"}
            if cmd in ("add", "group add"):
                return {"proj_item_add"}
            if cmd in ("rm", "group rm"):
                return {"proj_item_rm"}
            return {"proj_item_update"}
        if parts[0] == "changelog":
            return {f"proj_changelog_{parts[1].replace('-', '_')}"}
        rt, verb = parts[0], parts[1]
        if verb in ("new", "ls", "list"):
            return {f"proj_{verb}"} if verb != "list" else {"proj_ls"}
        if verb in ("show", "update", "verb"):
            return {"proj_show"} if verb == "show" else {"proj_update", "proj_verb"}
        # a type-specific verb (task move, ...)
        return {f"proj_{rt}_{verb}"}

    def walk(c, prefix=""):
        for name, sub in sorted(c.commands.items()):
            path = (prefix + " " + name).strip()
            if is_group(sub):
                yield from walk(sub, path)
            else:
                yield path

    missing = []
    for path in walk(typer.main.get_command(app)):
        for tool in expected(path):
            if tool not in tools:
                missing.append((path, tool))
    assert not missing, f"CLI commands without an MCP tool: {missing}"
