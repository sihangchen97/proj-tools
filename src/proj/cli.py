"""Typer CLI for proj (v3: SQLite core, YAML templates, md input channel)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import typer

from proj import changelog as cl
from proj import decision as dec
from proj import plan as planmod
from proj import records as rec
from proj import scaffold
from proj import task as taskmod
from proj.check import check as run_checks
from proj.config import ProjConfig, load_config
from proj.hooks import install as hooks_install

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


@app.callback()
def _main(root: Path | None = typer.Option(
        None, "--root", help="project root (default: discover by walking up)")):
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
def new(kind: str, title: str,
        supersede: str | None = typer.Option(None, "--supersede", "-s"),
        plan: str | None = typer.Option(None, "--plan", help="owning plan id (tasks)"),
        field: list[str] = typer.Option(
            [], "--field", help="kind field: name=value (repeatable)"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Don't open editor")):
    """Create a new record from its template (DB insert + render md)."""
    cfg = _cfg()
    fields = {}
    for f in field:
        if "=" not in f:
            _fail(f"--field expects name=value, got {f!r}")
        k, v = f.split("=", 1)
        fields[k.strip()] = v.strip()
    if kind == "task" and plan:
        fields["plan"] = plan
    if kind not in rec.load_registry(cfg.root):
        _fail(f"unknown kind {kind!r} (run `proj info` for registered kinds)")
    try:
        if kind == "decision" and supersede:
            _, p = dec.new(cfg, title, supersede)
        else:
            _, p = rec.new_record(cfg, kind, title, fields=fields or None)
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
def render(kind: str | None = None, id_: str | None = None):
    """DB → md (no md input). No args = every record + views."""
    cfg = _cfg()
    try:
        if kind is None:
            written = rec.render_all(cfg)
            typer.echo(f"Rendered {len(written)} file(s)")
            return
        spec = rec.load_registry(cfg.root)[kind]
        db = rec.open_db(cfg)
        if id_ is None:
            rows = db.all(spec)
        else:
            row = rec.by_id(cfg, spec, db, id_)
            if row is None:
                db.close()
                _fail(f"{kind} {id_} not found")
            rows = [row]
        for row in rows:
            p = rec.file_path(cfg, spec, row)
            p.write_text(rec.render_record(spec, row, db), encoding="utf-8")
            typer.echo(f"Rendered {p.relative_to(cfg.root)}")
        db.close()
    except KeyError as e:
        _fail(f"unknown kind {e.args[0]!r}")


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
def list_cmd(kind: str, status: str | None = typer.Option(None, "--status")):
    """List records of a kind (from DB)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    if kind not in reg:
        _fail(f"unknown kind {kind!r} (registered: {', '.join(sorted(reg))})")
    db = rec.open_db(cfg)
    rows = db.all(reg[kind])
    db.close()
    if status:
        rows = [r for r in rows if r["status"] == status]
    if not rows:
        typer.echo(f"(no {kind} records)" + (f" with status {status!r}" if status else ""))
    for r in rows:
        typer.echo(f"{r['id']:<8} {r['status']:<18} {r['title']}")


@app.command()
def show(kind: str, id_: str,
         history: bool = typer.Option(False, "--history", help="full changelog")):
    """Show one record (from DB)."""
    cfg = _cfg()
    reg = rec.load_registry(cfg.root)
    if kind not in reg:
        _fail(f"unknown kind {kind!r}")
    spec = reg[kind]
    db = rec.open_db(cfg)
    row = rec.by_id(cfg, spec, db, id_)
    if row is None:
        db.close()
        _fail(f"{kind} {id_} not found")
    typer.echo(f"id: {row['id']}")
    typer.echo(f"uuid: {row['uuid']}")
    typer.echo(f"title: {row['title']}")
    typer.echo(f"status: {row['status']}")
    typer.echo(f"created: {row['created']}")
    for f in spec.fields:
        if row.get(f.name):
            typer.echo(f"{f.name}: {row[f.name]}")
    logs = db.logs(kind, row["uuid"])
    db.close()
    if history:
        for e in logs:
            typer.echo(f"  {e['date']}  [{e['category']}] {e['summary']}")


@app.command()
def status(kind: str, id_: str, new_status: str):
    """Set a record's status (validated against the template's allowed set)."""
    cfg = _cfg()
    try:
        row = rec.status_set(cfg, kind, id_, new_status)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"{kind} {row['id']}: status → {new_status}")


@app.command()
def log(kind: str, id_: str, summary: str):
    """Append a changelog entry: "Category: one-line summary" (or plain text)."""
    cfg = _cfg()
    try:
        rec.add_log(cfg, kind, id_, summary)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"logged on {kind} {id_}")


# ---- decision ------------------------------------------------------------------------

@decision_app.command("new")
def decision_new(title: str,
                 supersede: str | None = typer.Option(None, "--supersede", "-s"),
                 yes: bool = typer.Option(False, "--yes", "-y")):
    """Create the next decision (with optional supersede)."""
    cfg = _cfg()
    try:
        _, p = dec.new(cfg, title, supersede)
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
    """Regenerate .proj/toc/decisions.md."""
    cfg = _cfg()
    out = cfg.proj_dir / "toc" / "decisions.md"
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
def task_new(title: str, plan: str | None = typer.Option(None, "--plan"),
             yes: bool = typer.Option(False, "--yes", "-y")):
    """Create a task (optionally attached to a plan)."""
    cfg = _cfg()
    try:
        _, p = taskmod.new(cfg, title, plan)
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
        plan = f"  (plan {r['plan']})" if r.get("plan") else ""
        typer.echo(f"{r['id']:<8} {r['status']:<12} {r['title']}{plan}")


# ---- plan --------------------------------------------------------------------------------

@plan_app.command("new")
def plan_new(title: str, yes: bool = typer.Option(False, "--yes", "-y")):
    """Create a plan (Draft)."""
    cfg = _cfg()
    try:
        _, p = planmod.new(cfg, title)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Created {p.relative_to(cfg.root)}")
    if not yes:
        _editor(p)


for _verb, _desc in [
    ("activate", "Draft → Active (its tasks appear in the main tasklist)"),
    ("review", "Active → Pending review (requires all its tasks Done)"),
    ("shelve", "Pending review → Shelved (its tasks park in the plan)"),
    ("resume", "Shelved → Active"),
    ("archive", "Pending review → Archived (frozen)"),
]:
    def _make(verb=_verb, desc=_desc):
        @plan_app.command(verb)
        def cmd(id_: str):
            """desc"""
            cfg = _cfg()
            try:
                row = planmod.transition(cfg, verb, id_)
            except ValueError as e:
                _fail(str(e))
            typer.echo(f"plan {row['id']}: status → {row['status']}")
    _make()


@plan_app.command("list")
def plan_list(status: str | None = typer.Option(None, "--status")):
    """List plans."""
    cfg = _cfg()
    for r in planmod.list_plans(cfg, status):
        typer.echo(f"{r['id']:<8} {r['status']:<16} {r['title']}")


# ---- changelog ----------------------------------------------------------------------------

@cl_app.command("add")
def cl_add(entry: str, version: str = typer.Option("Unreleased", "--version")):
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
    """Install .proj/githooks/pre-commit (runs `proj check`) and core.hooksPath."""
    cfg = _cfg()
    hook = hooks_install(cfg)
    typer.echo(f"Installed {hook} — pre-commit now runs `proj check`")
