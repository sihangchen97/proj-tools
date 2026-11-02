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
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import typer
from typer.core import TyperGroup
from typer.main import get_command as _to_click_command

from proj import bugs, scaffold
from proj.check import check as run_checks
from proj.core import fsio
from proj.core.config import ProjConfig, find_root, load_config, load_project_env
from proj.core.templates import _registry_key, resolve_record_type
from proj.engine import item_ops, present
from proj.engine import store as rec
from proj.engine import wip as wip_ops
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


# the generated `proj <type>` sub-groups, keyed by (root, canonical, registry key)
_GROUP_CACHE: dict[tuple, object] = {}


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
        if canonical is None:
            return None
        # R4 顺带: typer rebuilds the whole sub-group on EVERY command (each
        # build re-resolves the root + registry and re-decorates new/ls/show +
        # every verb). The group depends only on (root, canonical, registry key)
        # and its command bodies read _ROOT fresh at run time — cache and reuse.
        root = find_root(_ROOT["root"], strict=False)
        key = (root, canonical, _registry_key(root, None))
        cached = _GROUP_CACHE.get(key)
        if cached is None:
            cached = _build_record_group(canonical)
            _GROUP_CACHE.clear()
            _GROUP_CACHE[key] = cached
        return cached


# non-record_type sub-apps (changelog + hooks are not record_type verbs)
cl_app = typer.Typer(add_completion=False, help="CHANGELOG.md maintenance.")
hooks_app = typer.Typer(add_completion=False, help="Git hooks.")
item_app = typer.Typer(add_completion=False,
                       help="Record items (checklists): ls / add / set / check / …")
wip_app = typer.Typer(add_completion=False,
                      help="WIP.md (what is being worked on): add / return / ls.")


# no no_args_is_help: a bare `proj` shows project status (handled in the callback).
# token_normalize_func: command names (record_types AND subcommands/verbs) are
# matched case-insensitively — `DEC NEW X`, `decision Accept` … (options too).
app = typer.Typer(
    cls=_DynamicGroup,
    context_settings={"token_normalize_func": str.lower},
    help="Project governance: decisions, tasks, plans, changelog, tech debt, "
         "incidents, experiments. Per type: `proj <type> new|ls|show|update|<verb>`; "
         "any record: `proj show|ls|open`; checklists: `proj item …`; "
         "keep files and database in line: `proj sync` / `proj check`.")
app.add_typer(cl_app, name="changelog")
app.add_typer(hooks_app, name="hooks")
app.add_typer(item_app, name="item")
app.add_typer(wip_app, name="wip")

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


@contextmanager
def _write_lock(strict: bool = True):
    """S.2: every write command holds the project write lock (fsio.write_atomic
    covers the file writes); read commands never take it. No project root → no
    lock (a failing command has nothing to guard)."""
    try:
        cfg = _cfg(strict)
    except FileNotFoundError:
        yield
        return
    with fsio.project_lock(cfg.root, " ".join(["proj", *sys.argv[1:]])):
        yield


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
    """Create a new record of `name` (writes its file)."""
    if not title:
        _fail(f"title required: proj {name} new \"Title\"")
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    flat_sup = [x.strip() for s in supersede for x in s.split(",") if x.strip()]
    flat_rel = [x.strip() for s in related for x in s.split(",") if x.strip()]
    # A3: --parent is only supported by task; passing it elsewhere is an error,
    # not a silent no-op (the option used to be registered on every type).
    if parent and name != "task":
        _fail(f"--parent is only supported by task (got {name})")
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
    # C11: agents (non-tty stdin) get no editor prompt; -y still always skips
    if not yes and sys.stdin.isatty():
        _editor(p)


def _dyn_list(name: str | None, status: str | None) -> list[dict]:
    """The records to list (from DB); `name` None = every type (`proj ls`).
    Data only — `_print_rows` prints them as text or JSON."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    names = [name] if name else list(reg)
    rows: list[dict] = []
    with rec.open_db(cfg) as db:  # R3: one read connection for the whole ls
        for n in names:
            for r in record_for(n, reg).list(cfg, status):
                parent = db.parent_of(r["id"])
                rows.append({"id": r["id"], "type": n, "status": r["status"],
                             "title": r["title"], "parent": (parent["source"] if parent else None)})
    return rows


def _print_rows(rows: list[dict], status: str | None, as_json: bool) -> None:
    """`ls` output: one line per record (id, status, title, parent), or — with
    --json — only a JSON array of the rows (nothing else, so it parses)."""
    if as_json:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for row in rows:
        parent = f"  (parent {row['parent']})" if row["parent"] else ""
        typer.echo(f"{row['id']:<8} {row['status']:<24} {row['title']}{parent}")
    if not rows:
        typer.echo("(no records)" + (f" with status {status!r}" if status else ""))


def _dyn_show(name: str | None, id_: str, history: bool, json_out: bool = False) -> None:
    """Show one record (from DB); `name` may be None — the id then resolves
    across every record_type (C5: `proj show T-000001`). Formatting is shared
    with the MCP server (engine/present.py, D5); with `json_out` the result is
    a JSON object (C10) and nothing else is printed."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    with rec.open_db(cfg) as db:  # R3: one read connection for the whole show
        spec, row = (reg[name], rec.by_id(cfg, reg[name], db, id_)) if name else \
            rec.ref_record(cfg, db, id_)
        if row is None:
            _fail(f"{(name + ' ' if name else '') + id_} not found")
        text, data = present.show(spec, row, db, history=history)
    if json_out:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    typer.echo(text)


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


def _verb_help(spec, verb: str) -> str:
    """C6: a verb's help generated from the profile — the state transition
    (from → to) plus the gate(s) it must pass, e.g.
    'In Progress → Done (gate: all tasks finished, all criteria closed)'."""
    frm, to = spec.verbs[verb]
    frm_txt = "Any" if frm == "Any" else (" / ".join(frm) if isinstance(frm, list) else frm)
    parts: list[str] = []
    if to in spec.gates_tasks:
        parts.append("all tasks finished")
    for it in spec.items:  # per-checklist gate (P1)
        if to in it.gate:
            noun = {"criterion": "criteria"}.get(it.noun, it.noun + "s")
            parts.append(f"all {noun} closed")
    gate = f" (gate: {', '.join(parts)})" if parts else ""
    return f"{frm_txt} → {to}{gate}"


def _build_record_group(name: str):
    """Generate the `proj <name>` sub-group: new / list / show + template verbs."""
    root = find_root(_ROOT["root"], strict=False)
    spec = rec.load_registry(root).get(name)
    sub = typer.Typer(add_completion=False,
                        help=f"{name} records: new / ls / show / update + its state changes.")

    @sub.command()
    def new(title: str | None = typer.Argument(None, help="record title"),
            supersede: list[str] = typer.Option(
                [], "--supersede", help="replaced ids, same type (repeatable; comma ok)"),
            parent: str | None = typer.Option(None, "--parent", help="parent id (task only)"),
            related: list[str] = typer.Option(
                [], "--related", "-r", help="related ids, any type (repeatable; comma ok)"),
            commit: str | None = typer.Option(None, "--commit", help="git commit hash"),
            summary: str | None = typer.Option(None, "--summary", "-s", help="one-line summary"),
            yes: bool = typer.Option(False, "--yes", "-y", help="don't open the editor")):
        """Create a new record of this type (opens the editor; -y skips it)."""
        _dyn_new(name, title, supersede, parent, related, commit, summary, yes)

    @sub.command()
    def ls(status: str | None = typer.Option(None, "--status"),
           as_json: bool = typer.Option(False, "--json", help="a JSON array")):
        """List the records of this type."""
        _print_rows(_dyn_list(name, status), status, as_json)

    @sub.command("list", hidden=True)
    def list_(status: str | None = typer.Option(None, "--status"),
              as_json: bool = typer.Option(False, "--json", help="a JSON array")):
        """(alias for `ls`, kept hidden for compatibility)"""
        _print_rows(_dyn_list(name, status), status, as_json)

    @sub.command()
    def show(ID: str, history: bool = typer.Option(False, "--history", help="full changelog"),
             as_json: bool = typer.Option(False, "--json", help="a JSON object")):
        """Show one record of this type."""
        _dyn_show(name, ID, history, json_out=as_json)

    @sub.command()
    def update(ID: str,
               title: str | None = typer.Option(None, "--title", "-t", help="new title"),
               summary: str | None = typer.Option(
                   None, "--summary", "-s", help="new one-line summary"),
               message: str | None = typer.Option(
                   None, "--message", "-m",
                   help="append a changelog entry ('Category: text')")):
        """Update a record's title / summary (+ optional changelog entry)."""
        _dyn_update(name, ID, title, summary, message)

    if name == "task":
        @sub.command()
        def move(ID: str = typer.Argument(..., help="task id to re-home"),
                 parent: str | None = typer.Option(
                     None, "--parent", help="new parent record id (any type)"),
                 detach: bool = typer.Option(
                     False, "--detach", help="remove the parent edge (→ Unparented)")):
            """Move an existing task under a new parent (or detach it)."""
            if bool(parent) == detach:
                _fail("pass exactly one of --parent <ID> / --detach")
            try:
                row = record_for("task", rec.load_registry(_cfg().root)).move(
                    _cfg(), ID, parent)
            except ValueError as e:
                _fail(str(e))
            typer.echo(f"{row['id']}: parent → {parent or 'none (Unparented)'}")

    if spec is not None:
        for verb in spec.verbs:
            def _make(v=verb):
                @sub.command(v)
                def cmd(ID: str,
                        title: str | None = typer.Option(None, "--title", "-t",
                                                        help="also set the title"),
                        summary: str | None = typer.Option(None, "--summary", "-s",
                                                           help="also set the summary"),
                        message: str | None = typer.Option(
                            None, "--message", "-m",
                            help="also append a changelog entry ('Category: text')")):
                    _dyn_verb(name, v, ID, title, summary, message)
                cmd.__doc__ = _verb_help(spec, v)  # C6: from the profile
            _make()
    return _to_click_command(sub)


# ---- lifecycle -----------------------------------------------------------------

@app.command()
def init(profiles: list[str] = typer.Option(
        ["default"], "--profile", help="default, cv, infra, quality (additive)"),
        zoo: bool = typer.Option(
        False, "--zoo", help="also drop the agent rule file (.roo/rules/proj-tools.md)")):
    """Create a proj project in this directory (or add profiles to an existing
    one): database, record folders, starter files; existing md files are adopted."""
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
    """Bring the md files and the DB in line: every hand edit is checked and
    stored, then the tool-owned parts of every file are regenerated. No file
    argument = everything."""
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
    """Take in existing md files as records (by the id marker on their first
    line if they have one, else by the id in the file name)."""
    cfg = _cfg()
    problems, notes = rec.adopt(cfg, list(files) if files else None)
    _print_problems(problems, notes)
    if problems:
        raise typer.Exit(1)
    typer.echo("OK — adopted and re-rendered")


@app.command()
def render(ref: list[str] = typer.Argument(
        None, help="record id or type (default: all)")):
    """Regenerate files from the DB (no md input). No args = every record +
    every view; otherwise just the named record or type."""
    # A1: render is a write command — ingest unsynced hand edits first so they are
    # never clobbered (problems abort before any file is written)
    cfg = _cfg()
    try:
        rec.ensure_synced(cfg)
        if not ref:
            written = rec.render_all(cfg)
            typer.echo(f"Rendered {len(written)} file(s)")
            return
        reg = rec.load_registry(cfg.root)
        with rec.open_db(cfg) as db:  # R3: one read connection (no DB writes)
            for r in ref:
                spec, row = rec.ref_record(cfg, db, r)
                if row is None:
                    if r not in reg:
                        _fail(f"unknown record or type {r!r}")
                    spec, rows = reg[r], db.all(reg[r])
                else:
                    rows = [row]
                for row in rows:
                    p = rec.file_path(cfg, spec, row)
                    fsio.write_atomic(p, rec.render_record(spec, row, db, reg))
                    typer.echo(f"Rendered {p.relative_to(cfg.root)}")
    except rec.ProjError as e:
        _fail(str(e))
    except ValueError as e:
        _fail(str(e))


@app.command()
def show(ID: str,
         history: bool = typer.Option(False, "--history"),
         as_json: bool = typer.Option(False, "--json", help="a JSON object")):
    """Show one record's current state; the id may be any type (T-/DEC-/...)."""
    _dyn_show(None, ID, history, json_out=as_json)


@app.command()
def ls(status: str | None = typer.Option(None, "--status"),
       type: str | None = typer.Option(None, "--type",
                                       help="only this type"),
       as_json: bool = typer.Option(False, "--json", help="a JSON array")):
    """List records of every type, or of one type with --type."""
    cfg = _cfg()
    if type is not None and type not in rec.load_registry(cfg.root):
        _fail(f"unknown type {type!r}")
    _print_rows(_dyn_list(type, status), status, as_json)


def _open_record(ID: str) -> None:
    """C5: open a record's md in $EDITOR, then sync it back (auto-ingest)."""
    cfg = _cfg()
    with rec.open_db(cfg) as db:  # R3: one read connection
        spec, row = rec.ref_record(cfg, db, ID)
        if row is None:
            _fail(f"{ID} not found")
        p = rec.file_path(cfg, spec, row)
    _editor(p)
    problems, notes = rec.sync(cfg, [p])
    _print_problems(problems, notes)
    if problems:
        raise typer.Exit(1)


@app.command()
def open(ID: str) -> None:
    """Open a record's md in $EDITOR; it is synced back when you close it."""
    _open_record(ID)


@app.command()
def check():
    """Check that the md files, the database and the profiles agree (the
    pre-commit hook runs this); exit 1 on any problem."""
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
# item's type/number (unsynced md edits are auto-ingested first, A1), the closed-record
# re-open and the due date; on
# success it re-renders and returns the record's fresh item list, which we echo verbatim.

@item_app.command("ls")
def item_ls(ref: str | None = typer.Argument(None, help="one record id (omit = every record)"),
            group: str | None = typer.Option(None, "--group", help="only items of group L"),
            open_only: bool = typer.Option(False, "--open", help="only unclosed items"),
            type_: str | None = typer.Option(None, "--type", help="only records of this type"),
            owner: str | None = typer.Option(None, "--owner", help="only items with this owner"),
            overdue: bool = typer.Option(False, "--overdue",
                                        help="only past-due unclosed items"),
            as_json: bool = typer.Option(False, "--json", help="a JSON array of objects")):
    """List items, one per line: ref (ID#A01), state, text, then owner and
    due when set — for one record, or across all. --json prints a JSON array
    of objects (ref, record, no, group, state, closed, text, note, owner, due)."""
    cfg = _cfg()
    try:
        if as_json:
            rows = item_ops.ls_rows(cfg, ref, group=group, open_only=open_only,
                                    record_type=type_, owner=owner, overdue=overdue)
            typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
            return
        lines = item_ops.ls(cfg, ref, group=group, open_only=open_only, record_type=type_,
                            owner=owner, overdue=overdue)
    except ValueError as e:
        _fail(str(e))
    if not lines:
        typer.echo("(no items)")
    else:
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
             due: str = typer.Option("", "--due", help="a YYYY-MM-DD date (if enabled)"),
             task: str = typer.Option("", "--task", help="reference this task (T-…; → T-xxxx)")):
    """Add an item to a record's list (auto-numbered; appended at the end of its group)."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.add(cfg, ref, text, group=group, after=after, state=state,
                                note=note, owner=owner, due=due, task=task).rstrip())
    except ValueError as e:
        _fail(str(e))


@item_app.command("set")
def item_set(item: str, state: str,
             note: str | None = typer.Option(None, "--note", help="also set the note"),
             commit: str = typer.Option("", "--commit", help="a git commit hash")):
    """Set an item's state (one of its checklist's states); optional note / commit."""
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
              due: str | None = typer.Option(None, "--due", help="new due date (empty clears)"),
              task: str | None = typer.Option(
                  None, "--task", help="referenced task (T-…; empty clears the reference)"),
              reassign: bool = typer.Option(
                  False, "--reassign",
                  help="hand the item to a different owner (or clear it); "
                       "refused without this when the item is owned")):
    """Edit an item's text / note / owner / due / task reference (empty clears a
    field). S.3: changing or clearing an owned item's owner needs --reassign."""
    cfg = _cfg()
    try:
        typer.echo(item_ops.edit(cfg, item, text=text, note=note, owner=owner, due=due,
                                 task=task, reassign=reassign).rstrip())
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

item_group_app = typer.Typer(add_completion=False,
                                  help="Manage a record's item groups (### L \u00b7 name).")
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


# ---- WIP.md (what is being worked on) ---------------------------------------------------

@wip_app.command("add")
def wip_add_cmd(ids: list[str] = typer.Argument(..., help="one or more record ids")):
    """Register records in WIP.md (the "in progress" list)."""
    cfg = _cfg()
    try:
        _, notes = wip_ops.wip_add(cfg, ids)
    except ValueError as e:
        _fail(str(e))
    for n in notes:  # a record already in WIP is skipped, not an error (§5.6)
        typer.echo(f"note: {n}")
    typer.echo("\n".join(f"{r['id']}  {r['status']}  {r['title']}"
                        for r in wip_ops.wip_ls(cfg)) or "(no records in WIP)")


@wip_app.command("return")
def wip_return_cmd(ids: list[str] = typer.Argument(..., help="one or more record ids")):
    """Remove records from WIP.md (writing any edited items back first)."""
    cfg = _cfg()
    try:
        returned, notes = wip_ops.wip_return(cfg, ids)
    except ValueError as e:
        _fail(str(e))
    for n in notes:
        typer.echo(f"note: {n}")
    typer.echo("\n".join(f"returned {r}" for r in returned))


@wip_app.command("ls")
def wip_ls_cmd(owner: str | None = typer.Option(
        None, "--owner",
        help="only WIP entries that carry an OPEN item owned by this person "
             "(lists those items)")):
    """The WIP list: ID  status  title  n/m. --owner filters to entries with an
    open item owned by that person and lists them."""
    cfg = _cfg()
    typer.echo(wip_ops.format_wip_ls(wip_ops.wip_ls(cfg, owner), owner))


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


# ---- bug reports ----------------------------------------------------------------------------

@app.command()
def bug(text: str | None = typer.Argument(None,
            help="report text; '-' reads from stdin; pass 'ls' to list reports"),
        cmd: str | None = typer.Option(None, "--cmd", help="the command being run"),
        as_json: bool = typer.Option(False, "--json", help="JSON output for 'ls'"),
        root: Path | None = typer.Option(None, "--root", help="project root")):
    """Record a bug in the proj tool itself -> .proj.bugs (B2); `proj bug ls` lists.

    The first argument is interpreted as the subcommand name `ls` (a group's
    positional would otherwise steal it) — `proj bug ls` / `proj bug ls --json`."""
    if text is None or text == "ls":
        recs = bugs.list_bugs(root)
        if as_json:
            typer.echo(json.dumps(recs, indent=2, ensure_ascii=False))
            return
        if not recs:
            typer.echo("(no bug reports)")
            return
        for r in recs:
            first = r["body"].splitlines()[0] if r["body"] else ""
            typer.echo(f"{r['time']} · {r['source']} · {r['reporter']} · {first}")
        return
    if text == "-":
        text = sys.stdin.read()
    path = bugs.append_bug(text, "cli", cmd=cmd, root=root)
    typer.echo(f"recorded to {path}")


def main():
    """CLI entry point (pyproject [project.scripts] proj = "proj.cli:main").

    B4: an uncaught, unexpected exception (not a normal ValueError / sqlite3 /
    typer error) is auto-recorded to .proj.bugs before the process exits
    non-zero; normal errors just exit non-zero."""
    try:
        app()
    except SystemExit:
        raise  # typer.Exit (normal errors, clean output) — not a bug
    except (ValueError, sqlite3.Error):
        raise typer.Exit(1) from sys.exc_info()[1]  # normal error — no auto-record
    except Exception as e:
        prog = os.path.basename(sys.argv[0]) if sys.argv else "proj"
        path = bugs.auto_record(e, "cli", cmd=" ".join([prog, *sys.argv[1:]]),
                                root=_ROOT["root"])
        typer.secho(f"unexpected error — recorded to {path}", fg="red", err=True)
        raise typer.Exit(1) from e
