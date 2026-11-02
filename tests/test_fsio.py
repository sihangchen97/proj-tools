"""S.2: atomic writes + the project write lock (core/fsio.py).

Standalone (no proj fixture): a bare temp dir stands in for the project root.
The two-process test uses a real subprocess (a separate PID's flock is what
this lock exists to serialize)."""
from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from proj.core import fsio
from proj.core.db import ProjError

# ---- write_atomic ---------------------------------------------------------------

def test_write_atomic_creates_and_writes(tmp_path):
    p = tmp_path / "sub" / "f.md"
    fsio.write_atomic(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_write_atomic_refuses_to_clobber_a_changed_file(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("original", encoding="utf-8")
    seen = fsio.mtime_of(p)
    time.sleep(0.01)  # make the next write land on a different mtime
    p.write_text("someone else's edit", encoding="utf-8")
    with pytest.raises(ProjError, match="modified while this command was running"):
        fsio.write_atomic(p, "my version", expect_mtime_ns=seen)
    assert p.read_text(encoding="utf-8") == "someone else's edit"


def test_write_atomic_replaces_when_mtime_matches(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("old", encoding="utf-8")
    seen = fsio.mtime_of(p)
    fsio.write_atomic(p, "new", expect_mtime_ns=seen)
    assert p.read_text(encoding="utf-8") == "new"


def test_write_atomic_error_leaves_no_tempfile_and_keeps_original(tmp_path, monkeypatch):
    p = tmp_path / "f.md"
    p.write_text("original", encoding="utf-8")
    seen = fsio.mtime_of(p)
    real_replace = fsio.os.replace

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(fsio.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        fsio.write_atomic(p, "new", expect_mtime_ns=seen)
    assert p.read_text(encoding="utf-8") == "original"
    # no leftover temp file in the directory
    assert [x.name for x in tmp_path.iterdir()] == ["f.md"]
    monkeypatch.setattr(fsio.os, "replace", real_replace)


# ---- project_lock ---------------------------------------------------------------

def test_project_lock_reentrant_in_one_thread(tmp_path):
    with fsio.project_lock(tmp_path, "proj item add"):
        assert (tmp_path / fsio.LOCK_NAME).is_file()
        with fsio.project_lock(tmp_path, "proj item add"):  # nested: no deadlock
            pass
    # released after the outermost exit: a fresh flock succeeds instantly
    with fsio.project_lock(tmp_path, "second"):
        pass


def test_project_lock_skips_a_non_project_dir(tmp_path):
    missing = tmp_path / "nope"
    with fsio.project_lock(missing, "cmd"):  # no dir → no lock file
        assert not (missing / fsio.LOCK_NAME).exists()


def test_project_lock_writes_holder_pid_and_command(tmp_path):
    with fsio.project_lock(tmp_path, "proj sync"):
        line = (tmp_path / fsio.LOCK_NAME).read_text(encoding="utf-8").strip()
    assert line == f"{__import__('os').getpid()} proj sync"


def _holder_process(hold_seconds: float, root: str, ready: str) -> None:
    with fsio.project_lock(root, "holder command"):
        Path(ready).touch()  # the parent can only start after this
        time.sleep(hold_seconds)


def _wait_ready(ready: Path, holder: multiprocessing.Process) -> None:
    deadline = time.monotonic() + 5.0
    while not ready.exists():
        if holder.exitcode is not None:
            raise AssertionError(f"the holder process died (exit {holder.exitcode})")
        if time.monotonic() >= deadline:
            raise AssertionError("the holder never took the lock")
        time.sleep(0.01)


def test_two_processes_second_waits_and_both_survive(tmp_path):
    """The second writer waits on the first's lock (flock across PIDs) and both
    commands complete — the second never overwrites the first's effect."""
    ready = tmp_path / ".holder-ready"
    holder = multiprocessing.Process(
        target=_holder_process, args=(1.0, str(tmp_path), str(ready)))
    holder.start()
    _wait_ready(ready, holder)  # the holder holds the lock now
    t0 = time.monotonic()
    with fsio.project_lock(tmp_path, "waiter command"):
        waited = time.monotonic() - t0
        assert waited >= 0.9, f"the waiter did not block on the other PID (waited {waited:.2f}s)"
    holder.join(5)
    assert holder.exitcode == 0
    # the waiter overwrote the holder's note (both ran, in order)
    line = (tmp_path / fsio.LOCK_NAME).read_text(encoding="utf-8").strip()
    assert "waiter command" in line


def test_project_lock_timeout_names_the_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(fsio, "LOCK_TIMEOUT", 0.5)
    ready = tmp_path / ".holder-ready"
    holder = multiprocessing.Process(
        target=_holder_process, args=(3.0, str(tmp_path), str(ready)))
    holder.start()
    _wait_ready(ready, holder)  # the holder holds the lock now
    try:
        with pytest.raises(ProjError, match=r"held by pid \d+ \(holder command\)"), \
                fsio.project_lock(tmp_path, "late command"):
            pass
    finally:
        holder.terminate()
        holder.join(5)


def test_stale_lock_file_is_not_fatal(tmp_path):
    """A dead holder's flock is released by the kernel — a leftover file must
    not block a fresh writer."""
    (tmp_path / fsio.LOCK_NAME).write_text("99999999 some dead command\n", encoding="utf-8")
    with fsio.project_lock(tmp_path, "fresh"):
        line = (tmp_path / fsio.LOCK_NAME).read_text(encoding="utf-8").strip()
    assert line.endswith("fresh")


def test_lock_timeout_is_ten_seconds():
    assert fsio.LOCK_TIMEOUT == 10.0


