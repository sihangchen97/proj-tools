"""Project-root discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
