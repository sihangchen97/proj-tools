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
    """Walk up from start (default cwd) looking for a project root.

    A root is a directory containing pyproject.toml, .git, or .proj.sqlite
    (created by `proj init`). The nearest match wins. With strict=False,
    fall back to the start directory (used by `init` to bootstrap a brand-new
    project).
    """
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists() \
                or (candidate / ".proj.sqlite").is_file():
            return candidate
    if not strict:
        return cur
    raise FileNotFoundError(f"no project root (pyproject.toml or .git) found from {cur}")


def load_config(start: Path | None = None, strict: bool = True) -> ProjConfig:
    return ProjConfig(root=find_root(start, strict=strict))
