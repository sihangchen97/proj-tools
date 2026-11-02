"""Hardened file IO (S.2): atomic writes + the project-level write lock.

`write_atomic` writes through a same-directory temp file and `os.replace`, so a
reader never sees a half-written file; given the mtime the caller saw before
starting (`expect_mtime_ns`), it refuses to clobber a file that changed in the
meantime (the original is kept and a ProjError is raised).

`project_lock` serializes the project's write commands: an exclusive
`fcntl.flock` on `<root>/.proj/write.lock` (pid + command written into it),
waited on up to `LOCK_TIMEOUT` seconds. Read commands take no lock; a lock held
by a dead process is released by the kernel, so a stale file is harmless.
"""
from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from proj.core.db import ProjError

#: the write lock (S.10): a runtime artifact, never committed (.proj/.gitignore)
LOCK_NAME = ".proj/write.lock"
LOCK_TIMEOUT = 10.0

_local = threading.local()


def mtime_of(path: str | Path) -> int | None:
    """st_mtime_ns; None when the file does not exist (a fresh creation)."""
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return None


def write_atomic(path: str | Path, text: str, expect_mtime_ns: int | None = None) -> None:
    """Atomically write `text` to `path` (temp file in the same dir, then
    os.replace). `expect_mtime_ns`: the mtime the caller saw before the command
    started — right before the replace, the file is re-stat'd; changed since
    then → ProjError and the original file is kept. An error while writing
    never leaves a temp file behind and never touches the original."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if (expect_mtime_ns is not None and path.exists()
                and mtime_of(path) != expect_mtime_ns):
            raise ProjError(f"{path.name} modified while this command was running "
                           f"- re-run it")
        # mkstemp creates the temp file 0600; keep the target's own mode (or
        # 0644 for a fresh file) so the replace does not silently make every
        # written md private.
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _holder(lock_path: Path) -> str:
    """'pid 12345 (proj item add …)' from the lock file, best-effort."""
    try:
        line = lock_path.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return "an unknown process"
    pid, _, cmd = line.partition(" ")
    return f"pid {pid} ({cmd})" if cmd else f"pid {pid}"


@contextmanager
def project_lock(root: str | Path, cmd: str = ""):
    """Hold the project's exclusive write lock (up to LOCK_TIMEOUT seconds);
    the timeout names the holder (pid + command) from the lock file.
    No project dir → no lock (a failing command has nothing to guard).
    Re-entrant per thread: the CLI entry lock and the engine's own lock nest in
    the same thread — flock is per open file description, so without this the
    nested acquisition would time out on its own lock; a nested call shares the
    outer flock and only the outermost exit releases it."""
    key = str(Path(root).resolve())
    if getattr(_local, "held", None) == key:
        yield
        return
    root = Path(root)
    if not root.is_dir():
        yield
        return
    lock_path = root / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)  # S.10: .proj/ may not exist
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + LOCK_TIMEOUT
    got = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError as e:  # lock contention (BlockingIOError) — poll
                if time.monotonic() >= deadline:
                    raise ProjError(
                        f"project write lock {lock_path} is held by "
                        f"{_holder(lock_path)} (waited {LOCK_TIMEOUT:.0f}s)") from e
                time.sleep(0.1)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()} {cmd}".rstrip().encode("utf-8") + b"\n")
        _local.held = key
        yield
    finally:
        if got:
            _local.held = None
            with suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
