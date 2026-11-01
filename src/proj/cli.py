"""Typer CLI for proj (SQLite core, YAML templates, md input channel).

Command surface:
    proj                          # bare = project status (or the only-`init` hint)
    proj <lifecycle>              # init / sync / adopt / update / render / check /
                                  # info / docs / changelog … / hooks …
    proj item ls|add|set|check|… # record checklists (the `items:` block of a type)
    proj <record_type> new …      # dynamic, generated per record_type at runtime
    proj <record_type> list …
    proj <record_type> show <ID>
    proj <record_type> <verb> <ID>   # verbs come from the record_type's template

IDs are `{code}-{digits}` (7 chars, e.g. DEC-0001, T-000001); the suffix is
DIGITS ONLY — there is no letter overflow (once the all-digit space is used up,
new ids raise). Any zero-shortened form resolves: "1", "0001", "T-1" and
"DEC-0001" all hit the same record.

Record_types are discovered from the template registry (only known once a project
exists), so `proj <record_type>` subcommands are built on demand by
`_DynamicGroup.get_command` — there is no static entry for each type.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import typer
from typer.core import TyperGroup
from typer.main import get_command as _to_click_command

from proj import scaffold
from proj.check import check as run_checks
from proj.core.config import ProjConfig, find_root, load_config, load_project_env
from proj.core.db import show_date
from proj.core.templates import resolve_record_type
from proj.engine import item_ops
from proj.engine import items as items_mod
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.hooks import install as hooks_install
from proj.record_types import changelog as cl

_ROOT: dict[str, Path | None] = {"root": None}  # set by the --root callback


def _version_callback(v: bool):
    if v:
        import importlib.metadata
        # distribution name (PyPI pkg is 'proj-tools'; import name stays 'proj')
        typer.echo(f"proj {importlib.metadata.version('proj-tools')}")
        raise typer.Exit()


def _resolve_type(name: str) -> str | None:
    """record_type name or its code, case-insensitive → canonical name (None if no).

    Uses the bundled+project registry best-effort; an uninit'd dir still yields
    the bundled names, and the command body then fails cleanly via `_cfg()`.
    """
    root = find_root(_ROOT["root"], strict=False)
    try:
        return resolve_record_type(name, rec.load_registry(root))
    except ValueError:
        return None


class _DynamicGroup(TyperGroup):
    """Top-level group that resolves `proj <record_type> …` on the fly.

    Static commands resolve first; an unknown name that resolves to a record_type
    (name or code, case-insensitive) becomes a generated sub-group (new / list /
    show + template verbs). The callback runs before command resolution, so
    `_ROOT` is set; `token_normalize_func` (lower) makes record_type AND subcommand
    names case-insensitive too.
    """

    def get_command(self, ctx, name):
        cmd = super().get_command(ctx, name)
        if cmd is not None:
            return cmd
        canonical = _resolve_type(name)
        if canonical is not None:
            return _build_record_group(canonical)
        return None


# non-record_type sub-apps (changelog + hooks are not record_type verbs)
cl_app = typer.Typer(help="CHANGELOG.md maintenance.")
hooks_app = typer.Typer(help="Git hooks.")
item_app = typer.Typer(help="Record items (checklists): ls / add / set / check / …")

# no no_args_is_help: a bare `proj` shows project status (handled in the callback).
# token_normalize_func: command names (record_types AND subcommands/verbs) are
# matched case-insensitively — `DEC NEW X`, `decision Accept` … (options too).
app = typer.Typer(
    cls=_DynamicGroup,
    context_settings={"token_normalize_func": str.lower},
    help="Project governance: decisions, tasks, plans, changelog, tech debt, "
         "incidents, experiments. `proj <record_type> new|list|show|<verb>` "
         "+ lifecycle commands (init/sync/…).")
app.add_typer(cl_app, name="changelog")
app.add_typer(hooks_app, name="hooks")
app.add_typer(item_app, name="item")

@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context,
          root: Path | None = typer.Option(
              None, "--root", help="project root (default: the current directory)"),
          version: bool = typer.Option(
              False, "--version", callback=_version_callback, is_eager=True,
              help="Print version and exit")):
    _ROOT["root"] = root
    # D6: the project .env supplies config defaults (OS env wins); loaded once
    # at the entry point, before any command resolves
    load_project_env(find_root(root, strict=False))
    if ctx.invoked_subcommand is None and not version:
        # bare `proj` = project status; an uninit dir gets the one-line `error:`
        # (strict discovery, no DB side effects)
        try:
            cfg = load_config(_ROOT["root"])
        except FileNotFoundError as e:
            _fail(str(e))
        lines = rec.info_text(rec.info(cfg)).rstrip().splitlines()
        typer.secho(lines[0], bold=True)  # the `Project: …` header line
        typer.echo("\n".join(lines[1:]))
        raise typer.Exit()


def _cfg(strict: bool = True) -> ProjConfig:
    try:
        return load_config(_ROOT["root"], strict=strict)
    except FileNotFoundError as e:
        _fail(str(e))


def _fail(msg: str) -> None:
    typer.secho("error:", fg="red", bold=True, nl=False, err=True)
    typer.echo(f" {msg}", err=True)
    raise typer.Exit(1)


def _print_problems(problems: list[str], notes: list[str]) -> None:
    for p in problems:
        typer.secho("FAIL", fg="red", bold=True, nl=False, err=True)
        typer.echo(f"  {p}", err=True)
    for n in notes:
        typer.echo(f"note: {n}")


def _editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.run([editor, str(path)], check=False)
    else:
        typer.echo(f"Created {path} (set $EDITOR to open automatically)")


# ---- dynamic `proj <record_type> …` ---------------------------------------------------

def _dyn_new(name: str, title: str | None, supersede: list[str], parent: str | None,
             related: list[str], commit: str | None, summary: str | None, yes: bool) -> None:
    """Create a new record of `name` (DB insert + render md)."""
    if not title:
        _fail(f"title required: proj {name} new \"Title\"")
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    flat_sup = [x.strip() for s in supersede for x in s.split(",") if x.strip()]
    flat_rel = [x.strip() for s in related for x in s.split(",") if x.strip()]
    obj = record_for(name, reg)
    try:
        if name == "task":
            _, p = obj.new(cfg, title, parent or None, summary or "",
                           commit=commit or "", related=flat_rel or None)
        else:
            _, p = obj.new(cfg, title, summary=summary or "", commit=commit or "",
                           supersede=flat_sup or None, related=flat_rel or None)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Created {p.relative_to(cfg.root)}")
    if not yes:
        _editor(p)


def _dyn_list(name: str, status: str | None) -> None:
    """List records of `name` (from DB)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    rec_obj = record_for(name, reg)
    rows = rec_obj.list(cfg, status)
    if not rows:
        typer.echo(f"(no {name} records)" + (f" with status {status!r}" if status else ""))
        return
    spec = rec_obj.spec
    db = rec.open_db(cfg)
    for r in rows:
        parent = db.parent_of(r["id"])
        parent = f"  (parent {parent['source']})" if parent else ""
        typer.echo(f"{r['id']:<8} {spec.status_display(r['status']):<24} {r['title']}{parent}")
    db.close()


def _dyn_show(name: str, id_: str, history: bool) -> None:
    """Show one record of `name` (from DB)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    spec = reg[name]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        _fail(f"{name} {id_} not found")
    typer.echo(f"id: {row['id']}")
    typer.echo(f"title: {row['title']}")
    typer.echo(f"status: {row['status']}")
    typer.echo(f"created: {show_date(row['created'])}")
    if row.get("one_sentence"):
        typer.echo(f"one_sentence: {row['one_sentence']}")
    if row.get("commit"):
        typer.echo(f"commit: {row['commit']}")
    if spec.record_type == "task":
        parent = db.parent_of(row["id"])
        if parent:
            typer.echo(f"parent: {parent['source']}")
    if spec.items is not None:
        state = items_mod.load(db, row["id"], spec)
        state.intro = items_mod._intro_of(spec, row)
        rendered = items_mod.render_section(state, spec)
        if rendered:
            typer.echo("")
            typer.echo(f"{spec.items.section}:")
            typer.echo(rendered)
    logs = db.logs(row["id"])
    db.close()
    if history:
        for e in logs:
            typer.echo(f"  {show_date(e['date'])}  [{e['category']}] "
                       f"{rec._log_body(e, spec)}")


def _dyn_verb(name: str, verb: str, id_: str, title: str | None, summary: str | None,
              message: str | None) -> None:
    """Run a template verb (a state-machine step); -t/-s/-m also update it after."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    try:
        row = record_for(name, reg).transition(cfg, verb, id_)
        if title is not None or summary is not None or message:
            row = rec.update_record(cfg, name, row["id"],
                                    title=title, summary=summary, message=message)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"{name} {row['id']}: status → {row['status']}")


def _dyn_update(name: str, id_: str, title: str | None, summary: str | None,
                message: str | None) -> None:
    """Update a record's title / summary (-m also appends a changelog entry)."""
    if title is None and summary is None and not message:
        _fail("nothing to update — pass -t TITLE / -s SUMMARY / -m MESSAGE")
    cfg = _cfg()
    try:
        row = rec.update_record(cfg, name, id_, title=title, summary=summary, message=message)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"{name} {row['id']}: title → {row['title']}")


def _build_record_group(name: str):
    """Generate the `proj <name>` sub-group: new / list / show + template verbs."""
    root = find_root(_ROOT["root"], strict=False)
    spec = rec.load_registry(root).get(name)
    sub = typer.Typer(help=f"{name} records: new / list / show + template verbs.")

    @sub.command()
    def new(title: str | None = typer.Argument(None, help="record title"),
            supersede: list[str] = typer.Option(
                [], "--supersede", "-s", help="replaced ids, same type (repeatable; comma ok)"),
            parent: str | None = typer.Option(None, "--parent", help="parent id (task only)"),
            related: list[str] = typer.Option(
                [], "--related", "-r", help="related ids, any type (repeatable; comma ok)"),
            commit: str | None = typer.Option(None, "--commit", help="git commit hash"),
            summary: str | None = typer.Option(None, "--summary", help="one-line summary"),
            yes: bool = typer.Option(False, "--yes", "-y", help="don't open the editor")):
        """Create a new record (DB insert + render md)."""
        _dyn_new(name, title, supersede, parent, related, commit, summary, yes)

    @sub.command("list")
    def list_(status: str | None = typer.Option(None, "--status")):
        """List records of this type (from DB)."""
        _dyn_list(name, status)

    @sub.command()
    def show(id_: str, history: bool = typer.Option(False, "--history", help="full changelog")):
        """Show one record of this type (from DB)."""
        _dyn_show(name, id_, history)

    @sub.command()
    def update(id_: str,
               title: str | None = typer.Option(None, "--title", "-t", help="new title"),
               summary: str | None = typer.Option(
                   None, "--summary", "-s", help="new one-line summary"),
               message: str | None = typer.Option(
                   None, "--message", "-m",
                   help="append a changelog entry ('Category: text')")):
        """Update a record's title / summary (+ optional changelog entry)."""
        _dyn_update(name, id_, title, summary, message)

    @sub.command()
    def render(id: str | None = typer.Argument(
            None, help="record id (bare number or full); omitted = all of this type")):
        """DB → md for this type (no md input)."""
        _render_type(_cfg(), name, id)

    if name == "task":
        @sub.command()
        def move(id_: str = typer.Argument(..., help="task id to re-home"),
                 parent: str | None = typer.Option(
                     None, "--parent", help="new parent record id (any type)"),
                 detach: bool = typer.Option(
                     False, "--detach", help="remove the parent edge (→ Unparented)")):
            """Move an existing task under a new parent (or detach it)."""
            if bool(parent) == detach:
                _fail("pass exactly one of --parent <ID> / --detach")
            try:
                row = record_for("task", rec.load_registry(_cfg().root)).move(
                    _cfg(), id_, parent)
            except ValueError as e:
                _fail(str(e))
            typer.echo(f"{row['id']}: parent → {parent or 'none (Unparented)'}")

    if spec is not None:
        for verb in spec.verbs:
            def _make(v=verb):
                @sub.command(v)
                def cmd(id_: str,
                        title: str | None = typer.Option(None, "--title", "-t",
                                                        help="also set the title"),
                        summary: str | None = typer.Option(None, "--summary", "-s",
                                                           help="also set the summary"),
                        message: str | None = typer.Option(
                            None, "--message", "-m",
                            help="also append a changelog entry ('Category: text')")):
                    """State-machine step (from the template verbs)."""
                    _dyn_verb(name, v, id_, title, summary, message)
            _make()
    return _to_click_command(sub)


# ---- lifecycle -----------------------------------------------------------------

@app.command()
def init(profiles: list[str] = typer.Option(
        ["default"], "--profile", help="default, cv, infra, quality (additive)"),
        zoo: bool = typer.Option(
        False, "--zoo", help="also drop the agent rule file (.roo/rules/proj-tools.md)")):
    """Scaffold, build tables (lock wins over templates), seed, adopt files."""
    cfg = _cfg(strict=False)
    try:
        created = scaffold.init(cfg, profiles, zoo=zoo)
    except ValueError as e:
        _fail(str(e))
    if created:
        typer.echo("Created:")
        for c in created:
            typer.echo(f"  {c}")
    else:
        typer.echo("Nothing to create — everything already exists.")


@app.command()
def sync(files: list[Path] = typer.Argument(
        None, help="Files (default: all managed dirs)")):
    """md → DB (validate, normalize) → re-render all md."""
    cfg = _cfg()
    try:
        problems, notes = rec.sync(cfg, list(files) if files else None)
    except ValueError as e:
        _fail(str(e))
    _print_problems(problems, notes)
    if problems:
        raise typer.Exit(1)
    typer.echo("OK — synced and re-rendered")


@app.command()
def adopt(files: list[Path] = typer.Argument(
        None, help="Files (default: all managed dirs)")):
    """Absorb pre-existing md files (id marker if present; else id check)."""
    cfg = _cfg()
    problems, notes = rec.adopt(cfg, list(files) if files else None)
    _print_problems(problems, notes)
    if problems:
        raise typer.Exit(1)
    typer.echo("OK — adopted and re-rendered")


@app.command()
def update():
    """sync all + re-render every view (TASKLIST.md, CHANGELOG.md, view sections)."""
    cfg = _cfg()
    problems, notes = rec.sync(cfg, None)
    _print_problems(problems, notes)
    if problems:
        raise typer.Exit(1)
    typer.echo("OK — updated")


def _render_type(cfg: ProjConfig, name: str, id_: str | None) -> None:
    """DB → md for one record (or all of a type). Shared by `proj render --record-type`
    and the per-type `proj <rt> render` (a bare number or full id, via by_id)."""
    spec = rec.load_registry(cfg.root)[name]
    db = rec.open_db(cfg)
    if id_ is None:
        rows = db.all(spec)
    else:
        row = rec.by_id(cfg, spec, db, id_)
        if row is None:
            db.close()
            _fail(f"{name} {id_} not found")
        rows = [row]
    for row in rows:
        p = rec.file_path(cfg, spec, row)
        p.write_text(rec.render_record(spec, row, db), encoding="utf-8")
        typer.echo(f"Rendered {p.relative_to(cfg.root)}")
    db.close()


@app.command()
def render(record_type: str | None = typer.Option(None, "--record-type"),
           id: str | None = typer.Option(None, "--id")):
    """DB → md (no md input). No args = every record + views; else one type/record."""
    cfg = _cfg()
    try:
        drift = rec.md_drift(cfg)
        if drift:
            typer.echo(f"warning: unsynced md edits will be overwritten by this "
                       f"render: {', '.join(drift)} — run `proj sync` first",
                       err=True)
        if record_type is None:
            written = rec.render_all(cfg)
            typer.echo(f"Rendered {len(written)} file(s)")
            return
        _render_type(cfg, record_type, id)
    except KeyError as e:
        _fail(f"unknown record_type {e.args[0]!r}")


@app.command()
def check():
    """Validate DB ↔ lock ↔ templates ↔ md invariants; exit 1 on failure."""
    cfg = _cfg()
    problems = run_checks(cfg)
    if problems:
        for p in problems:
            typer.secho("FAIL", fg="red", bold=True, nl=False, err=True)
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(1)
    typer.echo("OK")


@app.command()
def info(as_json: bool = typer.Option(False, "--json", help="machine-readable")):
    """What this directory manages + which verbs change it (for humans and MCP)."""
    cfg = _cfg()
    d = rec.info(cfg)
    if as_json:
        typer.echo(json.dumps(d, indent=2, ensure_ascii=False))
    else:
        typer.echo(rec.info_text(d).rstrip())


@app.command()
def docs():
    """Regenerate docs/CLI.md + docs/MCP.md from this code."""
    from proj.docs import write
    for rel in write(_ROOT["root"]):
        typer.echo(f"Wrote {rel}")


# ---- changelog (not a record_type) ----------------------------------------------------

@cl_app.command("add")
def cl_add(entry: str, version: str | None = typer.Option(
        None, "--version", help="default: the changelog's unreleased placeholder")):
    """Add an entry: "Category: text" (Added/Changed/Fixed/...; plain text ok)."""
    cfg = _cfg()
    try:
        cl.add(cfg, entry, version)
    except ValueError as e:
        _fail(str(e))
    typer.echo("Added entry")


@cl_app.command("version")
def cl_version(name: str | None = typer.Argument(None),
               bump: str | None = typer.Option(
                   None, "--bump", help="major | minor | patch (from latest)")):
    """Release: move Unreleased into a version (--version NAME or --bump LEVEL)."""
    cfg = _cfg()
    try:
        used = cl.version_bump(cfg, name, bump)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Released {used}")


@cl_app.command("release-notes")
def cl_notes(version: str):
    """Print one version's section (for PR/tag bodies)."""
    cfg = _cfg()
    try:
        typer.echo(cl.release_notes(cfg, version).rstrip())
    except ValueError as e:
        _fail(str(e))


@cl_app.command("list")
def cl_list():
    """List released versions (newest first)."""
    cfg = _cfg()
    for version, d in cl.show_versions(cfg):
        typer.echo(f"{version}  {d}")


# ---- items (record checklists) ----------------------------------------------------------
#
# ITEM is the per-record item ref `ID#nnn` (e.g. T-000001#002, zero-padded to 3 digits);
# `#` needs no quoting in zsh. The engine (engine/items.py) validates every ref, the
# item's type/number, unsynced md edits, closed-record re-open and the due date; on
# success it re-renders and returns the record's fresh item list, which we echo verbatim.

@item_app.command("ls")
def item_ls(ref: str | None = typer.Argument(None, help="one record id (omit = every record)"),
            group: str | None = typer.Option(None, "--group", help="only items of group L"),
            open_only: bool = typer.Option(False, "--open", help="only unclosed items"),
            type_: str | None = typer.Option(None, "--type", help="limit to one record_type"),
            owner: str | None = typer.Option(None, "--owner", help="only items with this owner"),
            overdue: bool = typer.Option(False, "--overdue",
                                        help="only past-due unclosed items")):
    """List items as `ID#A01  state  text  [owner]  [due]` (one record, or across all)."""
    cfg = _cfg()
    try:
        lines = item_ops.ls(cfg, ref, group=group, open_only=open_only, record_type=type_,
                            owner=owner, overdue=overdue)
    except ValueError as e:
        _fail(str(e))
    if not lines:
        typer.echo("(no items)")
        return
    typer.echo("\n".join(lines))


@item_app.command("add")
def item_add(ref: str, text: str,
             group: str | None = typer.Option(
                 None, "--group", help="add to group L (default: the ungrouped list)"),
             after: str | None = typer.Option(None, "--after", help="insert right after ITEM"),
             state: str | None = typer.Option(
                 None, "--state", help="state to start in (default: the first)"),
             note: str = typer.Option("", "--note", help="free text after ' — '"),
             owner: str = typer.Option("", "--owner", help="the owner field (if enabled)"),
             due: str = typer.Option("", "--due", help="a YYYY-MM-DD date (if enabled)")):
    """Add an item to a record's list (auto-numbered; appended at the end of its group)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.add(cfg, ref, text, group=group, after=after, state=state,
                                note=note, owner=owner, due=due).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_app.command("set")
def item_set(item: str, state: str,
             note: str | None = typer.Option(None, "--note", help="also set the note"),
             commit: str = typer.Option("", "--commit", help="a git commit hash")):
    """Set an item's state (e.g. met / missed / done / skipped); optional note/commit."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.set_state(cfg, item, state, note=note, commit=commit).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_app.command("check")
def item_check(item: str,
               note: str | None = typer.Option(None, "--note", help="also set the note"),
               commit: str = typer.Option("", "--commit", help="a git commit hash")):
    """Mark an item closed (the first closed state of its checklist)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.check(cfg, item, note=note, commit=commit).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_app.command("uncheck")
def item_uncheck(item: str):
    """Re-open an item (back to the first, unclosed state)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.uncheck(cfg, item).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_app.command("edit")
def item_edit(item: str,
              text: str | None = typer.Option(None, "--text", help="new item text (empty clears)"),
              note: str | None = typer.Option(None, "--note", help="new note (empty clears)"),
              owner: str | None = typer.Option(None, "--owner", help="new owner (empty clears)"),
              due: str | None = typer.Option(None, "--due", help="new due date (empty clears)")):
    """Edit an item's text / note / owner / due (pass an empty string to clear a field)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.edit(cfg, item, text=text, note=note, owner=owner, due=due).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_app.command("mv")
def item_mv(item: str,
            group: str | None = typer.Option(
                None, "--group", help="move into group L (fresh number)"),
            ungroup: bool = typer.Option(
                False, "--ungroup", help="move to the ungrouped list (fresh number)"),
            after: str | None = typer.Option(None, "--after", help="move right after ITEM"),
            first: bool = typer.Option(False, "--first", help="move to the top of its group")):
    """Move an item: exactly one of --group/--ungroup/--after/--first.
    Cross-group moves get a fresh number (the old one is void)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.move(
            cfg, item, group=group, ungroup=ungroup, after=after, first=first).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_app.command("rm")
def item_rm(item: str):
    """Remove an item (its number is never reused)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.remove(cfg, item).rstrip())
    except ValueError as e:
        _fail(str(e))


# ---- item groups ----------------------------------------------------------------------

item_group_app = typer.Typer(help="Manage a record's item groups (### L \u00b7 name).")
item_app.add_typer(item_group_app, name="group")


@item_group_app.command("add")
def item_group_add(ref: str, title: str):
    """Add a group (the tool assigns the next free letter A-Z; empty groups are kept)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.group_add(cfg, ref, title).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_group_app.command("rename")
def item_group_rename(ref: str, letter: str, title: str):
    """Rename a group (the letters are stable; only the name after ' \u00b7 ' changes)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.group_rename(cfg, ref, letter, title).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_group_app.command("rm")
def item_group_rm(ref: str, letter: str,
                  ungroup_items: bool = typer.Option(
                      False, "--ungroup-items",
                      help="keep the group's items as ungrouped (fresh numbers)"),
                  delete_items: bool = typer.Option(
                      False, "--delete-items", help="delete the group's items with it")):
    """Delete a group. When it still holds items, pass exactly one of
    --ungroup-items / --delete-items."""
    cfg = _cfg()
    mode = ("ungroup" if ungroup_items else "delete" if delete_items else None)
    if ungroup_items == delete_items:
        _fail("pass exactly one of --ungroup-items / --delete-items")
    try:
        typer.echo(item_ops.group_rm(cfg, ref, letter, mode).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_group_app.command("mv")
def item_group_mv(ref: str, letter: str,
                  after: str | None = typer.Option(None, "--after", help="after group B"),
                  first: bool = typer.Option(False, "--first", help="to the top")):
    """Move a group (its items keep their numbers): exactly one of --after/--first."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.group_mv(cfg, ref, letter, after=after, first=first).rstrip())
    except ValueError as e:
        _fail(str(e))


# ---- hooks ------------------------------------------------------------------------------

@hooks_app.command("install")
def hooks_install_cmd():
    """Install .git/hooks/pre-commit (runs `proj check`)."""
    cfg = _cfg()
    try:
        hook = hooks_install(cfg)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Installed {hook} — pre-commit now runs `proj check`")
