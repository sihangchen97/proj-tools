"""C6: `proj compact` — rewrite .proj/events.jsonl into snapshots, verified by
a full replay before the file is replaced (hard-parts C6).

The fixture builds the project state BY REPLAYING hand-written events (all
timestamps explicit — a CLI-built project has never been replayed, so its
state is not reconstructible from a log until C2); once DB == replay(events)
holds by construction, compact's replay-compare has something true to check.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from proj import compact as compact_mod
from proj import events as ev
from proj import replay as rp
from proj import scaffold
from proj.cli import app
from proj.core.config import ProjConfig
from proj.core.db import Db
from proj.engine.store import load_registry

runner = CliRunner()
ROOT_OPT = ["--root"]


def _init(tmp_path: Path, name: str = "p") -> Path:
    d = tmp_path / name
    cfg = ProjConfig(root=d)
    scaffold.init(cfg, ["default", "quality"])
    return d


def _clear_seed(d: Path) -> None:
    c = sqlite3.connect(d / ".proj" / "cache.sqlite")
    try:
        c.execute("DELETE FROM record_changelog")  # no FK cascade; db.delete does it too
        c.execute("DELETE FROM records")           # FK cascade: items/relations/wip
        c.execute("DELETE FROM changelog_entries")
        c.commit()
    finally:
        c.close()


def _write_events(d: Path, events: list[dict]) -> None:
    p = d / ".proj" / "events.jsonl"
    p.write_text("".join(ev.to_line(e) for e in events), encoding="utf-8")


def _repo(d: Path) -> None:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", *args], cwd=d, check=True, env={**os.environ, **env})


def _fixture_events() -> list[dict]:
    """A full state: two tasks (T-000001 is T-000002's parent), one done item,
    a tombstone-free log, WIP, and a project changelog entry — every timestamp
    explicit so two replays agree to the second."""
    a = "2026-01-01T00:00:00+00:00"
    u1, u2, i1 = "u" * 32, "v" * 32, "w" * 32
    return [
        ev.make("create", actor="a", at=1, seq=1, rec=u1, type="task",
                id="T-000001", title="A", status="Draft", summary="sa",
                created=a, sections={}),
        ev.make("item.add", actor="a", at=2, seq=1, rec=u1, item=i1,
                no="001", text="s1", state="open",
                section="Acceptance Criteria", created=a),
        ev.make("item.set", actor="a", at=3, seq=1, rec=u1, item=i1,
                fields={"state": "done"}, date=a),
        ev.make("set", actor="a", at=4, seq=1, rec=u1,
                fields={"status": "In Progress"}, date=a),
        ev.make("wip.add", actor="a", at=5, seq=1, rec=u1,
                source="manual", added_at=a),
        ev.make("create", actor="a", at=6, seq=1, rec=u2, type="task",
                id="T-000002", title="B", status="Draft", summary="sb",
                created=a, sections={}),
        ev.make("rel.add", actor="a", at=7, seq=1, rec=u1, target=u2,
                kind="parent"),
        ev.make("changelog.add", actor="a", at=8, seq=1, rec="",
                version="", category="Added", message="v1", date=a),
    ]


def test_compact_rewrites_to_snapshots_and_replays_equivalent(tmp_path):
    d = _init(tmp_path)
    _clear_seed(d)
    db = Db(d / ".proj" / "cache.sqlite")
    events = _fixture_events()
    with db:
        rp.replay(db, events, load_registry(d))   # DB == replay(events) by construction
    db.close()
    _write_events(d, events)

    cfg = ProjConfig(root=d)
    problems, _ = compact_mod.run_compact(cfg)
    assert problems == []
    new = ev.read(d / ".proj/events.jsonl")
    assert all(e["op"] == "snapshot" for e in new), new
    assert len(new) == 3            # two record snapshots + the project-level one
    assert any(not e["rec"] for e in new)
    # the new log reproduces the DB exactly (C6's guarantee)
    fresh = Db(tmp_path / "fresh.sqlite")
    try:
        rp.replay(fresh, new, load_registry(d))
        assert compact_mod._dump(fresh) == compact_mod._dump(Db(d / ".proj/cache.sqlite"))
    finally:
        fresh.close()
    # and the CLI surface works too
    r = runner.invoke(app, [*ROOT_OPT, str(d), "compact"])
    assert r.exit_code == 0 and "compacted" in r.output, r.output


def test_compact_refused_on_dirty_tree(tmp_path):
    d = _init(tmp_path)
    _repo(d)
    (d / "CHANGELOG.md").write_text("untouched\n", encoding="utf-8")
    cfg = ProjConfig(root=d)
    problems, _ = compact_mod.run_compact(cfg)
    assert problems and "not clean" in problems[0]
    r = runner.invoke(app, [*ROOT_OPT, str(d), "compact"])
    assert r.exit_code == 1 and "not clean" in r.output


def test_compact_refused_while_merge_in_progress(tmp_path):
    d = _init(tmp_path)
    _repo(d)
    (d / ".git" / "MERGE_HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    cfg = ProjConfig(root=d)
    problems, _ = compact_mod.run_compact(cfg)
    assert problems and "in progress" in problems[0]


def test_compact_with_no_events_yet(tmp_path):
    d = _init(tmp_path)
    cfg = ProjConfig(root=d)
    problems, notes = compact_mod.run_compact(cfg)
    assert problems == [] and any("no events.jsonl" in n for n in notes)


def test_mcp_compact_reports(tmp_path):
    d = _init(tmp_path)
    from proj import mcp
    out = mcp.proj_compact(root=str(d))
    assert out.startswith("OK —") and "no events.jsonl" in out
    _repo(d)
    (d / "CHANGELOG.md").write_text("dirty\n", encoding="utf-8")
    out = mcp.proj_compact(root=str(d))
    assert out.startswith("error:") and "not clean" in out
