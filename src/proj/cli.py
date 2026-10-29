"""Typer CLI for proj."""

from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

import typer

from proj import decision as dec
from proj import index as pindex
from proj.adr import by_number
from proj.changelog import add_entry, release_notes, version
from proj.check import check as run_checks
from proj.config import load_config
from proj.hooks import install as hooks_install
from proj.records import RecordSet
from proj.scaffold import init as scaffold_init
from proj.tasklist import (
    add_task,
    done_task,
    list_plans,
    new_plan,
    read_tasks,
    resume_plan,
    shelve_task,
)

app = typer.Typer(
    help="Project governance: ADRs, tasklists, changelogs, tech debt, incidents, experiments.",
    no_args_is_help=True,
)
adr_app = typer.Typer(help="Architecture Decision Records.")
task_app = typer.Typer(help="TASKLIST.md — work in progress right now.")
plan_app = typer.Typer(help="docs/plans/ — overall plans, can be shelved/resumed.")
cl_app = typer.Typer(help="CHANGELOG.md maintenance.")
debt_app = typer.Typer(help="Tech debt items.")
incident_app = typer.Typer(help="Incident post-mortems.")
experiment_app = typer.Typer(help="Experiment tracking.")
hooks_app = typer.Typer(help="Git hooks.")

app.add_typer(adr_app, name="decision")
app.add_typer(task_app, name="task")
app.add_typer(plan_app, name="plan")
app.add_typer(cl_app, name="changelog")
app.add_typer(debt_app, name="debt")
app.add_typer(incident_app, name="incident")
app.add_typer(experiment_app, name="experiment")
app.add_typer(hooks_app, name="hooks")


def _editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        typer.echo(f"Created {path} (set $EDITOR to open automatically)")
        return
    subprocess.run([editor, str(path)], check=False)


def _record_set_for(cfg, which: str) -> RecordSet:
    if which == "debt":
        return RecordSet(cfg.debt_dir, prefix="debt")
    if which == "incident":
        return RecordSet(cfg.incidents_dir, dated=True, prefix="incident")
    if which == "experiment":
        return RecordSet(cfg.experiments_dir, prefix="experiment")
    raise ValueError(which)


DEBT_TEMPLATE = """\
# {number}. {title}

Date: {date}

## Status

Proposed

## Location

[File / module where the hack lives]

## Reason

[Why this temporary workaround was introduced]

## Trigger for removal

[Condition under which this debt must be repaid]
"""

INCIDENT_TEMPLATE = """\
# {number}. {title}

Date: {date}

## Status

Open

## Summary

[What broke, when, impact]

## Root cause

## Actions taken

## Prevention rule

[Actionable rule added to AI guidelines to prevent repeat errors]
"""

EXPERIMENT_TEMPLATE = """\
# {number}. {title}

Date: {date}
Commit: {commit}

## Status

Running

## Hypothesis

## Hyperparameters

## Results / Metric deltas

"""


@app.command()
def init(tiers: list[str] = typer.Option(["core"], "--tier", help="core, cv, infra, quality")):
    """Scaffold governance files (idempotent, never overwrites)."""
    cfg = load_config(strict=False)
    created = scaffold_init(cfg, tiers)
    if created:
        typer.echo("Created:")
        for c in created:
            typer.echo(f"  {c}")
    else:
        typer.echo("Nothing to create — everything already exists.")


@app.command()
def check():
    """Validate governance invariants; exit 1 on failure."""
    cfg = load_config()
    problems = run_checks(cfg)
    if problems:
        for p in problems:
            typer.echo(f"FAIL  {p}", err=True)
        raise typer.Exit(1)
    typer.echo("OK")


@adr_app.command("new")
def adr_new(title: str, supersede: str | None = typer.Option(None, "--supersede", "-s"),
            yes: bool = typer.Option(False, "--yes", "-y", help="Don't open editor")):
    """Create the next decision (frontmatter record)."""
    cfg = load_config()
    try:
        rec = dec.new(cfg, title, supersede)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    typer.echo(f"Created {rec.path}")
    if not yes:
        _editor(rec.path)


@adr_app.command("list")
def adr_list(status: str | None = typer.Option(None, "--status")):
    """List decisions."""
    cfg = load_config()
    for rec in dec._records(cfg):
        if status and rec.status != status:
            continue
        typer.echo(rec.display)


@adr_app.command("show")
def adr_show(id_: str, history: bool = typer.Option(False, "--history", help="Show audit trail")):
    """Show one decision's frontmatter (and history)."""
    cfg = load_config()
    rec = pindex.by_id(cfg, "decision", id_)
    if rec is None:
        raise typer.BadParameter(f"decision {id_} not found")
    for k in ("id", "uuid", "title", "status", "created"):
        typer.echo(f"{k}: {rec.meta.get(k, '')}")
    if rec.meta.get("supersedes"):
        typer.echo(f"supersedes: {rec.meta['supersedes']}")
    if history:
        for h in rec.history:
            frm, to = h.get("from"), h.get("to")
            extra = f" ({frm} -> {to})" if frm or to else ""
            typer.echo(f"  {h['date']}  {h['event']}{extra}")


@adr_app.command("status")
def adr_status(id_: str, new_status: str = typer.Argument(None, help="New status value")):
    """Set a decision's status (validated against the template's allowed set)."""
    cfg = load_config()
    if not new_status:
        raise typer.BadParameter("usage: proj decision status ID NEW_STATUS")
    try:
        rec = dec.set_status(cfg, id_, new_status)
        rec.write()
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    typer.echo(f"{rec.path.name}: status -> {new_status}")


@adr_app.command("link")
def adr_link(target: str, links: list[str]):
    """Add See-also cross-links to a decision."""
    cfg = load_config()
    try:
        dec.link(cfg, target, links)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    typer.echo(f"Linked {', '.join(links)} -> DEC-{target}")


@adr_app.command("toc")
def adr_toc():
    """Regenerate the decisions table of contents into .proj/toc/decisions.md."""
    cfg = load_config()
    out = cfg.proj_dir / "toc" / "decisions.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dec.toc(cfg), encoding="utf-8")
    typer.echo(f"Wrote {out}")


@adr_app.command("graph")
def adr_graph(out: Path | None = typer.Argument(None, help="Output file (default stdout)")):
    """Emit a mermaid graph of supersede links."""
    cfg = load_config()
    text = dec.graph(cfg)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out}")
    else:
        typer.echo(text)


@app.command()
def submit(files: list[Path] = typer.Argument(None, help="Files (default: all managed dirs)")):
    """Validate record files and upsert them into .proj/index.json."""
    cfg = load_config()
    index = pindex.load(cfg)
    if files:
        recs = []
        for f in files:
            spec = _spec_for_path(cfg, f)
            rec = pindex.read_record(cfg, spec, f) if spec else None
            if rec is None:
                raise typer.BadParameter(f"{f}: not a frontmatter-managed record")
            recs.append(rec)
    else:
        recs = pindex.all_records(cfg)
    problems: list[str] = []
    for rec in recs:
        problems.extend(pindex.validate_record(cfg, rec, index))
    if problems:
        for p in problems:
            typer.echo(f"FAIL  {p}", err=True)
        raise typer.Exit(1)
    for rec in recs:
        pindex.upsert(cfg, rec)
    typer.echo(f"OK — {len(recs)} record(s) validated and indexed")


def _spec_for_path(cfg, path: Path):
    from proj.templates import load_registry
    for spec in load_registry(cfg.root).values():
        if path.parent == cfg.root / spec.dir:
            return spec
    return None


@app.command("update")
def update():
    """Full rescan: rebuild .proj/index.json from all record files."""
    cfg = load_config()
    index, problems = pindex.rebuild(cfg)
    for p in problems:
        typer.echo(f"WARN  {p}", err=True)
    total = sum(len(v) for v in index["records"].values())
    typer.echo(f"Indexed {total} record(s) across {len(index['records'])} kind(s)")
    if problems:
        raise typer.Exit(1)


@task_app.command("add")
def task_add(title: str):
    """Add a new active task to TASKLIST.md."""
    cfg = load_config()
    add_task(cfg, title)
    typer.echo(f"Added task '{title}'")


@task_app.command("done")
def task_done(title: str):
    """Archive a fully-completed task to docs/plans/."""
    cfg = load_config()
    archive = done_task(cfg, title)
    typer.echo(f"Archived to {archive}")


@task_app.command("list")
def task_list():
    """List active tasks."""
    cfg = load_config()
    _h, blocks = read_tasks(cfg)
    for t, _l, task in blocks:
        typer.echo(f"{task.progress:>6}  {t}")


@plan_app.command("new")
def plan_new(title: str):
    """Create a draft overall plan in docs/plans/."""
    cfg = load_config()
    p = new_plan(cfg, title)
    typer.echo(f"Created {p}")


@plan_app.command("shelve")
def plan_shelve(title: str):
    """Park an in-progress task from TASKLIST.md into docs/plans/."""
    cfg = load_config()
    p = shelve_task(cfg, title)
    typer.echo(f"Shelved to {p}")


@plan_app.command("resume")
def plan_resume(title: str):
    """Bring a plan from docs/plans/ back into TASKLIST.md as an active task."""
    cfg = load_config()
    resume_plan(cfg, title)
    typer.echo(f"Resumed '{title}' into TASKLIST.md")


@plan_app.command("list")
def plan_list():
    """List plans in docs/plans/ (newest first)."""
    cfg = load_config()
    for p, _title, note in list_plans(cfg):
        suffix = f"  ({note})" if note else ""
        typer.echo(f"{p.name}{suffix}")


@cl_app.command("version")
def cl_version(bump_: str | None = typer.Option(None, "--bump", help="major|minor|patch"),
               explicit: str | None = typer.Option(None, "--explicit", help="Version string")):
    """Create a new released version section."""
    cfg = load_config()
    v = version(cfg, part=bump_, explicit=explicit)
    typer.echo(f"Created version {v}")


@cl_app.command("add")
def cl_add(entry: str, version_name: str = typer.Option("Unreleased", "--version")):
    """Add an entry. Format: '<Section> <text>', e.g. 'Added new feature'."""
    cfg = load_config()
    parts = entry.split(" ", 1)
    if len(parts) != 2:
        raise typer.BadParameter("format: '<Section> <text>'")
    add_entry(cfg, parts[0], parts[1], version_name)
    typer.echo("Added entry")


@cl_app.command("release-notes")
def cl_release_notes(version_name: str):
    """Print release notes for one version."""
    cfg = load_config()
    typer.echo(release_notes(cfg, version_name), nl=False)


@debt_app.command("new")
def debt_new(title: str, yes: bool = typer.Option(False, "--yes", "-y")):
    """Create a new tech-debt item."""
    cfg = load_config()
    rs = _record_set_for(cfg, "debt")
    number = rs.next_number()
    body = DEBT_TEMPLATE.format(number=number, title=title, date=date.today().isoformat())
    path = rs.create(title, body, number=number)
    typer.echo(f"Created {path}")
    if not yes:
        _editor(path)


@debt_app.command("list")
def debt_list():
    """List tech-debt items."""
    cfg = load_config()
    for rec in _record_set_for(cfg, "debt").discover():
        typer.echo(rec.display)


@debt_app.command("repay")
def debt_repay(number: str):
    """Mark a tech-debt item as Repaid."""
    cfg = load_config()
    rs = _record_set_for(cfg, "debt")
    rec = by_number(rs, number)
    if rec is None:
        raise typer.BadParameter(f"debt item {number} not found")
    rs.set_status(rec, f"Repaid on {date.today().isoformat()}")
    typer.echo(f"Repaid {rec.path.name}")


@incident_app.command("new")
def incident_new(title: str, yes: bool = typer.Option(False, "--yes", "-y")):
    """Create a new incident post-mortem."""
    cfg = load_config()
    rs = _record_set_for(cfg, "incident")
    number = rs.next_number()
    body = INCIDENT_TEMPLATE.format(number=number, title=title, date=date.today().isoformat())
    path = rs.create(title, body, number=number)
    typer.echo(f"Created {path}")
    if not yes:
        _editor(path)


@incident_app.command("list")
def incident_list():
    """List incidents."""
    cfg = load_config()
    for rec in _record_set_for(cfg, "incident").discover():
        typer.echo(rec.display)


@experiment_app.command("new")
def experiment_new(title: str, commit: str = typer.Option("", "--commit", "-c"),
                   yes: bool = typer.Option(False, "--yes", "-y")):
    """Create a new experiment record."""
    cfg = load_config()
    rs = _record_set_for(cfg, "experiment")
    number = rs.next_number()
    body = EXPERIMENT_TEMPLATE.format(number=number, title=title,
                                      date=date.today().isoformat(), commit=commit or "unknown")
    path = rs.create(title, body, number=number)
    typer.echo(f"Created {path}")
    if not yes:
        _editor(path)


@experiment_app.command("list")
def experiment_list():
    """List experiments."""
    cfg = load_config()
    for rec in _record_set_for(cfg, "experiment").discover():
        typer.echo(rec.display)


@experiment_app.command("close")
def experiment_close(number: str):
    """Mark an experiment as Closed."""
    cfg = load_config()
    rs = _record_set_for(cfg, "experiment")
    rec = by_number(rs, number)
    if rec is None:
        raise typer.BadParameter(f"experiment {number} not found")
    rs.set_status(rec, f"Closed on {date.today().isoformat()}")
    typer.echo(f"Closed {rec.path.name}")


@hooks_app.command("install")
def hooks_install_cmd():
    """Install git hooks (pre-commit runs `proj check`)."""
    cfg = load_config()
    hook = hooks_install(cfg)
    typer.echo(f"Installed {hook} and set core.hooksPath")


if __name__ == "__main__":
    app()
