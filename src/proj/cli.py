"""Typer CLI for proj (v3: SQLite core, YAML templates, md input channel)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import typer

from proj import scaffold
from proj.check import check as run_checks
from proj.core.config import ProjConfig, load_config
from proj.engine import store as rec
from proj.engine.record import record_for
from proj.hooks import install as hooks_install
from proj.record_types import changelog as cl
from proj.record_types import decision as dec
from proj.record_types import task as taskmod

# plan has no Record subclass: its state machine comes straight from the template yaml
def _plan(cfg):
    return record_for("plan", rec.load_registry(cfg.root))

app = typer.Typer(
    help="Project governance: decisions, tasks, plans, changelog, tech debt, "
         "incidents, experiments. SQLite core + YAML templates + md input channel.",
    no_args_is_help=True)
decision_app = typer.Typer(help="Decisions (ADR-style).")
task_app = typer.Typer(help="Tasks (main tasklist / plan tasks).")
plan_app = typer.Typer(help="Plans (state machine drives task visibility).")
cl_app = typer.Typer(help="CHANGELOG.md maintenance.")
hooks_app = typer.Typer(help="Git hooks.")

app.add_typer(decision_app, name="decision")
app.add_typer(task_app, name="task")
app.add_typer(plan_app, name="plan")
app.add_typer(cl_app, name="changelog")
app.add_typer(hooks_app, name="hooks")


_ROOT: dict[str, Path | None] = {"root": None}  # set by --root callback (MCP reuses)


def _version_callback(v: bool):
    if v:
        import importlib.metadata
        # distribution name (PyPI pkg is 'proj-tools'; import name stays 'proj')
        typer.echo(f"proj {importlib.metadata.version('proj-tools')}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context,
          root: Path | None = typer.Option(
              None, "--root", help="project root (default: discover by walking up)"),
          version: bool = typer.Option(
              False, "--version", callback=_version_callback, is_eager=True,
              help="Print version and exit")):
    if ctx.invoked_subcommand is None and not version:
        ctx.get_help()
        raise typer.Exit()
    _ROOT["root"] = root


def _cfg(strict: bool = True) -> ProjConfig:
    return load_config(_ROOT["root"], strict=strict)


def _fail(msg: str) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(1)


def _print_problems(problems: list[str], notes: list[str]) -> None:
    for p in problems:
        typer.echo(f"FAIL  {p}", err=True)
    for n in notes:
        typer.echo(f"note: {n}")


def _editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.run([editor, str(path)], check=False)
    else:
        typer.echo(f"Created {path} (set $EDITOR to open automatically)")


# ---- lifecycle -----------------------------------------------------------------

@app.command()
def init(profiles: list[str] = typer.Option(
        ["core"], "--profile", help="core, cv, infra, quality")):
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
def new(record_type: str, title: str,
        supersede: list[str] = typer.Option(
            [], "--supersede", "-s",
            help="id of a decision this one replaces (repeatable; comma also ok)"),
        parent: str | None = typer.Option(
            None, "--parent", help="parent record id, any type (tasks: plan/task/…)"),
        field: list[str] = typer.Option(
            [], "--field", help="record_type field: name=value (repeatable)"),
        summary: str | None = typer.Option(
            None, "--summary", help="one-sentence summary (result / progress)"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Don't open editor")):
    """Create a new record from its template (DB insert + render md)."""
    cfg = _cfg()
    fields = {}
    for f in field:
        if "=" not in f:
            _fail(f"--field expects name=value, got {f!r}")
        k, v = f.split("=", 1)
        fields[k.strip()] = v.strip()
    if record_type not in rec.load_registry(cfg.root):
        _fail(f"unknown record_type {record_type!r} (run `proj info` for registered record_types)")
    try:
        if record_type == "decision" and supersede:
            flat: list[str] = []
            for s in supersede:
                flat += [x.strip() for x in s.split(",") if x.strip()]
            _, p = dec.new(cfg, title, flat, summary or "")
        elif record_type == "task":
            _, p = taskmod.new(cfg, title, parent, summary or "")
        else:
            _, p = rec.new_record(cfg, record_type, title,
                                  fields=fields or None, summary=summary or "")
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Created {p.relative_to(cfg.root)}")
    if not yes:
        _editor(p)


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
    """Absorb pre-existing md files (uuid preferred; else id check + generate)."""
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


@app.command()
def render(record_type: str | None = None, id_: str | None = None):
    """DB → md (no md input). No args = every record + views."""
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
        spec = rec.load_registry(cfg.root)[record_type]
        db = rec.open_db(cfg)
        if id_ is None:
            rows = db.all(spec)
        else:
            row = rec.by_id(cfg, spec, db, id_)
            if row is None:
                db.close()
                _fail(f"{record_type} {id_} not found")
            rows = [row]
        for row in rows:
            p = rec.file_path(cfg, spec, row)
            p.write_text(rec.render_record(spec, row, db), encoding="utf-8")
            typer.echo(f"Rendered {p.relative_to(cfg.root)}")
        db.close()
    except KeyError as e:
        _fail(f"unknown record_type {e.args[0]!r}")


@app.command()
def check():
    """Validate DB ↔ lock ↔ templates ↔ md invariants; exit 1 on failure."""
    cfg = _cfg()
    problems = run_checks(cfg)
    if problems:
        for p in problems:
            typer.echo(f"FAIL  {p}", err=True)
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


# ---- generic record verbs ---------------------------------------------------------

@app.command("list")
def list_cmd(record_type: str, status: str | None = typer.Option(None, "--status")):
    """List records of a record_type (from DB)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    if record_type not in reg:
        _fail(f"unknown record_type {record_type!r} (registered: {', '.join(sorted(reg))})")
    rec_obj = record_for(record_type, reg)
    rows = rec_obj.list(cfg, status)
    if not rows:
        typer.echo(f"(no {record_type} records)" + (f" with status {status!r}" if status else ""))
    spec = rec_obj.spec
    for r in rows:
        typer.echo(f"{r['id']:<8} {spec.status_display(r['status']):<24} {r['title']}")


@app.command()
def show(record_type: str, id_: str,
         history: bool = typer.Option(False, "--history", help="full changelog")):
    """Show one record (from DB)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    if record_type not in reg:
        _fail(f"unknown record_type {record_type!r}")
    spec = reg[record_type]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        _fail(f"{record_type} {id_} not found")
    typer.echo(f"id: {row['id']}")
    typer.echo(f"uuid: {row['uuid']}")
    typer.echo(f"title: {row['title']}")
    typer.echo(f"status: {row['status']}")
    typer.echo(f"created: {row['created']}")
    for f in spec.fields:
        if row.get(f.name):
            typer.echo(f"{f.name}: {row[f.name]}")
    logs = db.logs(record_type, row["uuid"])
    db.close()
    if history:
        for e in logs:
            typer.echo(f"  {e['date']}  [{e['category']}] {e['summary']}")


@app.command()
def status(record_type: str, id_: str, new_status: str):
    """Set a record's status (validated against the template's allowed set)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    if record_type not in reg:
        _fail(f"unknown record_type {record_type!r}")
    try:
        row = record_for(record_type, reg).set_status(cfg, id_, new_status)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"{record_type} {row['id']}: status → {new_status}")


@app.command()
def verb(record_type: str, name: str, id_: str):
    """Run a template verb (a state-machine step; `from: Any` = from any status)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    if record_type not in reg:
        _fail(f"unknown record_type {record_type!r}")
    try:
        row = record_for(record_type, reg).transition(cfg, name, id_)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"{record_type} {row['id']}: status → {row['status']}")


@app.command()
def log(record_type: str, id_: str, summary: str):
    """Append a changelog entry: "Category: one-line summary" (or plain text)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    if record_type not in reg:
        _fail(f"unknown record_type {record_type!r}")
    try:
        record_for(record_type, reg).add_log(cfg, id_, summary)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"logged on {record_type} {id_}")


# ---- decision ------------------------------------------------------------------------

@decision_app.command("new")
def decision_new(title: str,
                 supersede: str | None = typer.Option(None, "--supersede", "-s"),
                 summary: str | None = typer.Option(None, "--summary"),
                 yes: bool = typer.Option(False, "--yes", "-y")):
    """Create the next decision (with optional supersede)."""
    cfg = _cfg()
    flat = [x.strip() for x in (supersede or "").split(",") if x.strip()]
    try:
        _, p = dec.new(cfg, title, flat or None, summary or "")
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Created {p.relative_to(cfg.root)}")
    if not yes:
        _editor(p)


@decision_app.command("list")
def decision_list(status: str | None = typer.Option(None, "--status")):
    """List decisions."""
    cfg = _cfg()
    rows = dec.records(cfg)
    if status:
        rows = [r for r in rows if r["status"] == status]
    for r in rows:
        typer.echo(f"{r['id']:<8} {r['status']:<22} {r['title']}")


@decision_app.command("toc")
def decision_toc():
    """Regenerate docs/decisions/_TOC.md."""
    cfg = _cfg()
    out = cfg.root / "docs" / "decisions" / "_TOC.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dec.toc(cfg), encoding="utf-8")
    typer.echo(f"Wrote {out.relative_to(cfg.root)}")


@decision_app.command("graph")
def decision_graph(out: Path | None = typer.Argument(
        None, help="Output file (default stdout)")):
    """Emit a mermaid graph of supersede links."""
    cfg = _cfg()
    text = dec.graph(cfg)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out}")
    else:
        typer.echo(text)


@decision_app.command("link")
def decision_link(target: str, links: list[str]):
    """Add See-also cross-references to a decision's Consequences."""
    cfg = _cfg()
    try:
        dec.link(cfg, target, links)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Linked {', '.join('DEC-' + n for n in links)} → DEC-{target}")


# ---- task ------------------------------------------------------------------------------

@task_app.command("new")
def task_new(title: str, parent: str | None = typer.Option(None, "--parent"),
             summary: str | None = typer.Option(None, "--summary"),
             yes: bool = typer.Option(False, "--yes", "-y")):
    """Create a task (optionally attached to a parent record)."""
    cfg = _cfg()
    try:
        _, p = taskmod.new(cfg, title, parent, summary or "")
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Created {p.relative_to(cfg.root)}")
    if not yes:
        _editor(p)


@task_app.command("done")
def task_done(id_: str):
    """Mark a task Done (requires every step + sub-task checked)."""
    cfg = _cfg()
    try:
        row = taskmod.done(cfg, id_)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"task {row['id']} → Done")


@task_app.command("list")
def task_list(status: str | None = typer.Option(None, "--status")):
    """List tasks (main + parked)."""
    cfg = _cfg()
    for r in taskmod.list_tasks(cfg, status):
        parent = f"  (parent {r['parent']})" if r.get("parent") else ""
        typer.echo(f"{r['id']:<8} {r['status']:<12} {r['title']}{parent}")


# ---- plan --------------------------------------------------------------------------------

@plan_app.command("new")
def plan_new(title: str, summary: str | None = typer.Option(None, "--summary"),
             yes: bool = typer.Option(False, "--yes", "-y")):
    """Create a plan (Draft)."""
    cfg = _cfg()
    try:
        _, p = _plan(cfg).new(cfg, title, summary or "")
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Created {p.relative_to(cfg.root)}")
    if not yes:
        _editor(p)


for _verb, _desc in [
    ("activate", "Draft → Active (its tasks appear in the main tasklist)"),
    ("review", "Active → Pending Review (requires all its tasks Done)"),
    ("shelve", "Pending Review → Shelved (its tasks park in the plan)"),
    ("resume", "Shelved → Active"),
    ("archive", "Pending Review → Archived (frozen)"),
    ("freeze", "Shelved → Archived (frozen)"),
]:
    def _make(verb=_verb, desc=_desc):
        @plan_app.command(verb)
        def cmd(id_: str):
            """desc"""
            cfg = _cfg()
            try:
                row = _plan(cfg).transition(cfg, verb, id_)
            except ValueError as e:
                _fail(str(e))
            typer.echo(f"plan {row['id']}: status → {row['status']}")
    _make()


@plan_app.command("list")
def plan_list(status: str | None = typer.Option(None, "--status")):
    """List plans."""
    cfg = _cfg()
    for r in _plan(cfg).list(cfg, status):
        typer.echo(f"{r['id']:<8} {r['status']:<16} {r['title']}")


# ---- changelog ----------------------------------------------------------------------------

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


# ---- hooks ---------------------------------------------------------------------------------

@hooks_app.command("install")
def hooks_install_cmd():
    """Install .git/hooks/pre-commit (runs `proj check`)."""
    cfg = _cfg()
    try:
        hook = hooks_install(cfg)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Installed {hook} — pre-commit now runs `proj check`")
