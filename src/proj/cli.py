"""Typer CLI for proj (v3: SQLite core, YAML templates, md input channel).

Command surface:
    proj                          # bare = project status (or the only-`init` hint)
    proj <lifecycle>              # init / sync / adopt / update / render / check /
                                  # info / docs / changelog … / hooks …
    proj <record_type> new …      # dynamic, generated per record_type at runtime
    proj <record_type> list …
    proj <record_type> show <ID>
    proj <record_type> <verb> <ID>   # verbs come from the record_type's template

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
    logs = db.logs(row["id"])
    db.close()
    if history:
        for e in logs:
            change = rec._log_change(e, spec)
            typer.echo(f"  {show_date(e['date'])}  [{e['category']}] {change}")


def _dyn_verb(name: str, verb: str, id_: str, title: str | None, summary: str | None,
              comment: str | None) -> None:
    """Run a template verb (a state-machine step); -t/-s/-c also update it after."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    try:
        row = record_for(name, reg).transition(cfg, verb, id_)
        if title is not None or summary is not None or comment:
            row = rec.update_record(cfg, name, row["id"],
                                    title=title, summary=summary, comment=comment)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"{name} {row['id']}: status → {row['status']}")


def _dyn_update(name: str, id_: str, title: str | None, summary: str | None,
                comment: str | None) -> None:
    """Update a record's title / summary (-c also appends a changelog entry)."""
    if title is None and summary is None and not comment:
        _fail("nothing to update — pass -t TITLE / -s SUMMARY / -c COMMENT")
    cfg = _cfg()
    try:
        row = rec.update_record(cfg, name, id_, title=title, summary=summary, comment=comment)
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
               comment: str | None = typer.Option(None, "--comment", "-c",
                                                  help="append a changelog entry")):
        """Update a record's title / summary (+ optional changelog entry)."""
        _dyn_update(name, id_, title, summary, comment)

    @sub.command()
    def render(id: str | None = typer.Argument(None, help="record id (bare number or full); omitted = all of this type")):
        """DB → md for this type (no md input)."""
        _render_type(_cfg(), name, id)

    if spec is not None:
        for verb in spec.verbs:
            def _make(v=verb):
                @sub.command(v)
                def cmd(id_: str,
                        title: str | None = typer.Option(None, "--title", "-t",
                                                        help="also set the title"),
                        summary: str | None = typer.Option(None, "--summary", "-s",
                                                           help="also set the summary"),
                        comment: str | None = typer.Option(None, "--comment", "-c",
                                                           help="also append a changelog entry")):
                    """State-machine step (from the template verbs)."""
                    _dyn_verb(name, v, id_, title, summary, comment)
            _make()
    return _to_click_command(sub)


# ---- lifecycle -----------------------------------------------------------------

@app.command()
def init(profiles: list[str] = typer.Option(
        ["default"], "--profile", help="default, cv, infra, quality (additive)")):
    """Scaffold, build tables (lock wins over templates), seed, adopt files."""
    cfg = _cfg(strict=False)
    try:
        created = scaffold.init(cfg, profiles)
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
