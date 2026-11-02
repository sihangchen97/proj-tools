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
    and the `.proj.bugs` records, so a changelog line names who did it."""
    return os.environ.get("PROJ_AGENT") or getpass.getuser() or "unknown"


@dataclass
class ProjConfig:
    root: Path

    @property
    def db_path(self) -> Path:
        # DB at the project root — committed by default (clone = ready)
        return self.root / ".proj.sqlite"


def find_root(start: Path | None = None, strict: bool = True) -> Path:
    """Project root = the start directory (default: cwd) if it is an
    initialized project, i.e. it holds a `.proj.lock` or `.proj.sqlite`
    (both created by `proj init`). No walking up: run `proj` from the
    project root, or pass `--root` for an explicit path. With
    strict=False, fall back to the start directory unconditionally (used
    by `init` to bootstrap a brand-new project).
    """
    cur = (start or Path.cwd()).resolve()
    if (cur / ".proj.lock").is_file() or (cur / ".proj.sqlite").is_file():
        return cur
    if not strict:
        return cur
    raise FileNotFoundError(
        f"no proj project in {cur} (needs .proj.lock / .proj.sqlite) — "
        f"run `proj init` here, or pass --root")


def load_config(start: Path | None = None, strict: bool = True) -> ProjConfig:
    return ProjConfig(root=find_root(start, strict=strict))


def load_project_env(root: Path) -> None:
    """Load `<root>/.env` into os.environ (D6): flat KEY=VALUE lines, `#`
    comments and blank lines skipped, **existing OS env vars are never
    overridden** (OS env > .env > code default). Idempotent; a missing file
    is a no-op. ~15 lines, no dotenv dependency."""
    env = root / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v
