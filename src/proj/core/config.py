"""Project-root discovery + the project .env (D6: all config is env vars;
the .env only supplies project-level defaults)."""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from pathlib import Path


def reporter() -> str:
    """S.4: the agent that made a change — `PROJ_AGENT` (set when an agent
    launches the CLI/MCP) else the system username ('unknown' as a last
    resort). Written into every `record_changelog` row (the `actor` column)
    and the `.proj/bugs.log` records, so a changelog line names who did it."""
    agent = os.environ.get("PROJ_AGENT")
    if agent:
        return agent
    try:
        return getpass.getuser() or "unknown"
    except (KeyError, OSError):
        # P1-5: no USER env / a broken pwd lookup used to raise HERE instead
        # of the docstring's promised 'unknown' fallback
        return "unknown"


#: the project data directory (S.10): cache.sqlite (committed until the git
#: collab C4 turns it into a rebuilt cache), lock.yaml (committed), bugs.log /
#: write.lock (never committed, .proj/.gitignore)
PROJ_DIR = ".proj"
#: the marker that a directory is a project root (S.10)
LOCK = f"{PROJ_DIR}/lock.yaml"


@dataclass
class ProjConfig:
    root: Path

    @property
    def proj_dir(self) -> Path:
        return self.root / PROJ_DIR

    @property
    def db_path(self) -> Path:
        # .proj/cache.sqlite — committed by default (clone = ready); the git
        # collab C4 later stops committing it (rebuilt from events.jsonl)
        return self.root / PROJ_DIR / "cache.sqlite"


def find_root(start: Path | None = None, strict: bool = True) -> Path:
    """Project root = the nearest ancestor of the start directory (default:
    cwd, or the explicit --root value) that is an initialized project, i.e.
    holds `.proj/lock.yaml` (created by `proj init`). S.10: walking up is
    allowed — commands work from the project's subdirectories. With
    strict=False, fall back to the start directory unconditionally (used
    by `init` to bootstrap a brand-new project).
    """
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / LOCK).is_file():
            return d
    if not strict:
        return cur
    raise FileNotFoundError(
        f"no proj project in {cur} or any ancestor (needs .proj/lock.yaml) — "
        f"run `proj init` here, or pass --root")


def load_config(start: Path | None = None, strict: bool = True) -> ProjConfig:
    return ProjConfig(root=find_root(start, strict=strict))


def load_project_env(root: Path) -> None:
    """Load `<root>/.env` into os.environ (D6): flat KEY=VALUE lines, `#`
    comments and blank lines skipped, **existing OS env vars are never
    overridden** (OS env > .env > code default). Re-calling with the SAME
    root is idempotent; across ROOTS in one long-lived process (e.g. the MCP
    server serving several projects) the first root loaded wins any
    conflicting key — its value is already in os.environ, so the later root's
    line is skipped (拍板 2026-11-03: documented, not changed). A missing
    file is a no-op. ~15 lines, no dotenv dependency."""
    env = root / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        # P1-6: a quoted value (`KEY="v"`) and a trailing ` # comment` are
        # stripped — a raw store would keep the quotes / comment in the value
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        else:
            v = v.split(" #", 1)[0].strip()
        if k and k not in os.environ:
            os.environ[k] = v
