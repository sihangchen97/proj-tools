"""S.9: task state transitions carry source restrictions (profiles/task.yaml verbs).

  start:  Draft, Failed          -> In Progress  (retry after Failed)
  done:   In Progress            -> Done         (no Draft -> Done; the first
                                                checked criterion auto-promotes
                                                to In Progress)
  fail:   Draft, In Progress, Done -> Failed     (an error found after Done can
                                                fail directly)
  reopen: Done                   -> In Progress  (keep working, without failing)

`reopen` has no module-level function (task.py stays start/done/fail); it is a
pure template verb, reached the same way the CLI and `proj_verb` reach every
verb — `record_for(...).transition(cfg, verb, id)`. Each verb pins one legal and
one illegal origin.
"""
import pytest

from proj.engine import item_ops as ops
from proj.engine import store as rec
from proj.engine.record import record_for


def _task(proj, title):
    row, _p = rec.new_record(proj, "task", title)
    return row["id"]


def _status(proj, id_):
    reg = rec.load_registry(proj.root)
    with rec.open_db(proj) as db:
        return db.get(reg["task"], id_)["status"]


def _verb(proj, verb, id_):
    # the generic entry the CLI/MCP use (works for every template verb, incl. reopen)
    return record_for("task", rec.load_registry(proj.root)).transition(proj, verb, id_)


def _logs(proj, id_):
    with rec.open_db(proj) as db:
        return db.logs(id_)


def test_start_legal_from_draft(proj):
    row, _ = rec.new_record(proj, "task", "S1")
    _verb(proj, "start", row["id"])
    assert _status(proj, row["id"]) == "In Progress"


def test_start_legal_retry_from_failed(proj):
    id_ = _task(proj, "S2")
    rec.status_set(proj, "task", id_, "Failed")
    _verb(proj, "start", id_)  # retry
    assert _status(proj, id_) == "In Progress"


def test_start_illegal_from_in_progress(proj):
    id_ = _task(proj, "S3")
    rec.status_set(proj, "task", id_, "In Progress")
    with pytest.raises(Exception, match=r"start requires"):
        _verb(proj, "start", id_)
    assert _status(proj, id_) == "In Progress"


def test_done_illegal_from_draft(proj):
    # a task with no criteria and no children: Draft -> done is refused (S.9)
    id_ = _task(proj, "D1")
    assert _status(proj, id_) == "Draft"
    with pytest.raises(Exception, match=r"done requires"):
        _verb(proj, "done", id_)
    assert _status(proj, id_) == "Draft"


def test_done_legal_from_in_progress(proj):
    id_ = _task(proj, "D2")
    rec.status_set(proj, "task", id_, "In Progress")
    _verb(proj, "done", id_)  # no criteria / no children -> the gate passes
    assert _status(proj, id_) == "Done"


def test_first_checked_criterion_auto_promotes(proj):
    # the mechanism S.9 relies on: Draft auto-promotes on the first closed item,
    # after which `done` is legal (the alternative to an explicit `start`)
    id_ = _task(proj, "P1")
    ops.add(proj, id_, "criterion a")
    ops.check(proj, f"{id_}#001")
    assert _status(proj, id_) == "In Progress"
    _verb(proj, "done", id_)
    assert _status(proj, id_) == "Done"


def test_fail_legal_from_draft_and_writes_changelog(proj):
    id_ = _task(proj, "F1")
    before = len(_logs(proj, id_))
    _verb(proj, "fail", id_)
    assert _status(proj, id_) == "Failed"
    assert len(_logs(proj, id_)) == before + 1  # the transition is logged


def test_fail_legal_from_done(proj):
    # the user requirement: an error found after Done fails directly
    id_ = _task(proj, "F2")
    rec.status_set(proj, "task", id_, "Done")
    _verb(proj, "fail", id_)
    assert _status(proj, id_) == "Failed"


def test_fail_illegal_from_failed(proj):
    id_ = _task(proj, "F3")
    rec.status_set(proj, "task", id_, "Failed")
    with pytest.raises(Exception, match=r"fail requires"):
        _verb(proj, "fail", id_)
    assert _status(proj, id_) == "Failed"


def test_reopen_legal_from_done(proj):
    # Done -> reopen -> In Progress (keep working without marking it failed)
    id_ = _task(proj, "R1")
    rec.status_set(proj, "task", id_, "Done")
    _verb(proj, "reopen", id_)
    assert _status(proj, id_) == "In Progress"


def test_reopen_illegal_from_in_progress(proj):
    id_ = _task(proj, "R2")
    rec.status_set(proj, "task", id_, "In Progress")
    with pytest.raises(Exception, match=r"reopen requires"):
        _verb(proj, "reopen", id_)
    assert _status(proj, id_) == "In Progress"
