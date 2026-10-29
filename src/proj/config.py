"""Project-root discovery and [tool.proj] config."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tomllib


@dataclass
class ProjConfig:
    root: Path
    adr_dir: Path = field(init=False)
    plans_dir: Path = field(init=False)
    changelog: Path = field(init=False)
    debt_dir: Path = field(init=False)
    incidents_dir: Path = field(init=False)
    experiments_dir: Path = field(init=False)
    proj_dir: Path = field(init=False)  # .proj/ — tool-owned files
    tiers: list[str] = field(default_factory=lambda: ["core"])

    def __post_init__(self) -> None:
        self.adr_dir = self.root / "docs/decisions"  # v2: decisions (was ADRs)
        self.plans_dir = self.root / "docs/plans"
        self.changelog = self.root / "CHANGELOG.md"
        self.debt_dir = self.root / "docs/tech-debt"
        self.incidents_dir = self.root / "docs/incidents"
        self.experiments_dir = self.root / "docs/experiments"
        self.proj_dir = self.root / ".proj"

    @property
    def db_path(self) -> Path:
        return self.proj_dir / "sqlite"


def find_root(start: Path | None = None, strict: bool = True) -> Path:
    """Walk up from start (default cwd) looking for a project root.

    A root is a directory containing pyproject.toml, .git, or .proj/
    (marker created by `proj init`). The nearest match wins. With
    strict=False, fall back to the start directory (used by `init` to
    bootstrap a brand-new project).
    """
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists() \
                or (candidate / ".proj").is_dir():
            return candidate
    if not strict:
        return cur
    raise FileNotFoundError(f"no project root (pyproject.toml or .git) found from {cur}")


def load_config(start: Path | None = None, strict: bool = True) -> ProjConfig:
    root = find_root(start, strict=strict)
    cfg = ProjConfig(root=root)
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        section = data.get("tool", {}).get("proj", {})
        cfg.tiers = list(section.get("tiers", ["core"]))
    return cfg
