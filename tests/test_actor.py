"""S.4: every changelog row records WHO made the change (PROJ_AGENT / username),
shown in the md changelog line, `show --history`, and `--json`."""
import os

import pytest

from proj.core.config import reporter
from proj.engine import store as rec
from proj.record_types import task as taskmod


@pytest.fixture
def _agent(monkeypatch):
    """Snapshot + clear PROJ_AGENT so each test sets it explicitly (and an unset
    case falls back to the real system username, not a leaked test value)."""
    saved = os.environ.pop("PROJ_AGENT", None)
    yield
    if saved is not None:
        os.environ["PROJ_AGENT"] = saved
    else:
        os.environ.pop("PROJ_AGENT", None)


def test_reporter_prefers_proj_agent(_agent, monkeypatch):
    monkeypatch.setenv("PROJ_AGENT", "alex")
    assert reporter() == "alex"


def test_reporter_falls_back_to_username(_agent, monkeypatch):
    monkeypatch.delenv("PROJ_AGENT", raising=False)
    # the real fallback is the system username (getpass.getuser) — assert it is
    # non-empty and not the agent name
    assert reporter() and reporter() != "alex"


def test_add_log_records_actor(proj, _agent, monkeypatch):
    monkeypatch.setenv("PROJ_AGENT", "alex")
    row, _ = taskmod.new(proj, "Feat")
    taskmod.done(proj, row["id"])
    db = rec.open_db(proj)
    logs = db.logs(row["id"])
    db.close()
    # the status-change row (and the create row) both carry the actor
    assert all(e["actor"] == "alex" for e in logs)


def test_add_log_falls_back_to_username(proj, _agent, monkeypatch):
    monkeypatch.delenv("PROJ_AGENT", raising=False)
    row, _ = taskmod.new(proj, "Feat")
    taskmod.done(proj, row["id"])
    db = rec.open_db(proj)
    logs = db.logs(row["id"])
    db.close()
    assert all(e["actor"] for e in logs)  # the system username, non-empty


def test_changelog_md_line_shows_actor(proj, _agent, monkeypatch):
    monkeypatch.setenv("PROJ_AGENT", "billy")
    row, _ = taskmod.new(proj, "Feat")
    taskmod.done(proj, row["id"])
    md = rec.file_path(proj, rec.load_registry(proj.root)["task"], row)
    text = md.read_text(encoding="utf-8")
    # the Done changelog line carries `· billy`
    assert any("· billy" in ln for ln in text.splitlines()
               if ln.strip().startswith("- `"))


def test_show_history_and_json_carry_actor(proj, _agent, monkeypatch):
    monkeypatch.setenv("PROJ_AGENT", "sam")
    row, _ = taskmod.new(proj, "Feat")
    taskmod.done(proj, row["id"])
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    from proj.engine import present
    text, data = present.show(spec, rec.by_id(proj, spec, db, row["id"]), db,
                              history=True)
    db.close()
    assert any("· sam" in ln for ln in text.splitlines())
    assert all(e["actor"] == "sam" for e in data["changelog"])


def test_old_rows_without_actor_render_unchanged(proj, _agent, monkeypatch):
    """A row written before S.4 (actor='') renders with no trailing `· ` suffix."""
    monkeypatch.setenv("PROJ_AGENT", "alex")
    row, _ = taskmod.new(proj, "Feat")
    spec = rec.load_registry(proj.root)["task"]
    db = rec.open_db(proj)
    # simulate a pre-S.4 row: clear its actor, then re-render
    db.conn.execute("UPDATE record_changelog SET actor='' WHERE record_id=?",
                    (row["id"],))
    logs = db.logs(row["id"])
    db.close()
    for e in logs:
        line = rec._log_entry(e, spec)
        assert "· alex" not in line
