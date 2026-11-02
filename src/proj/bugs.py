"""`.proj.bugs` — bug reports about the proj tool itself (PLAN §19, A13).

Plain-text, append-only, DB-free: a broken database must never stop a report
from being written. One `os.write` per record (O_APPEND) so concurrent writers
don't interleave. Auto-records uncaught exceptions (source `auto`).
"""
from __future__ import annotations

import getpass
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from proj import __version__
from proj.core.config import find_root

BUGS_FILE = ".proj.bugs"


def _reporter() -> str:
    return os.environ.get("PROJ_AGENT") or getpass.getuser() or "unknown"


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bug_path(root: str | Path | None = None) -> Path:
    """The `.proj.bugs` path: the project root when one is found (cwd or `root`),
    else the current directory (B5)."""
    base = find_root(Path(root) if root else None, strict=False)
    return Path(base or os.getcwd()) / BUGS_FILE


def append_bug(text: str, source: str, cmd: str | None = None,
               root: str | Path | None = None) -> Path:
    """Append one record to `.proj.bugs`; returns the file path. `text` is
    stripped; a body line starting with `=== ` gets two leading spaces so it can
    never read as the next record's header (B2/§19.2). One O_APPEND os.write."""
    path = bug_path(root)
    lines = [f"=== {_utc()} · proj {__version__} · {source} · {_reporter()}"]
    if cmd:
        lines.append(f"cmd: {cmd}")
    lines.extend("  " + ln if ln.startswith("=== ") else ln
                 for ln in text.strip().splitlines())
    data = "\n".join(lines) + "\n\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def auto_record(exc: BaseException, source: str, cmd: str,
                root: str | Path | None = None) -> Path:
    """Record an uncaught (unexpected) exception: source `auto`, the command (or
    MCP tool + args) and the full traceback (B4). Returns the file path."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return append_bug(tb.strip(), "auto", cmd=cmd, root=root)


def list_bugs(root: str | Path | None = None) -> list[dict]:
    """Parse `.proj.bugs` back into records (time / source / reporter / cmd /
    first body line / body). Returns [] when the file does not exist."""
    path = bug_path(root)
    if not path.is_file():
        return []
    out: list[dict] = []
    cur: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("=== "):
            head = line[4:]
            bits = [b.strip() for b in head.split("·")]
            ver = bits[1][len("proj "):] if len(bits) > 1 and bits[1].startswith("proj ") \
                   else (bits[1] if len(bits) > 1 else "")
            cur = {"time": bits[0], "version": ver,
                   "source": bits[2] if len(bits) > 2 else "",
                   "reporter": bits[3] if len(bits) > 3 else "",
                   "cmd": "", "body": []}
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("cmd: "):
            cur["cmd"] = line[len("cmd: "):]
        else:
            cur["body"].append(line[2:] if line.startswith("  ") else line)
    for r in out:
        r["body"] = "\n".join(r["body"]).strip()
    return out


def count(root: str | Path | None = None) -> int:
    """Number of records in `.proj.bugs` (0 when the file does not exist)."""
    path = bug_path(root)
    if not path.is_file():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines()
               if ln.startswith("=== "))
