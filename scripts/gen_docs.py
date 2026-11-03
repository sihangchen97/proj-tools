#!/usr/bin/env python3
"""`pixi run docs` (PLAN §28.1 23:10): regenerate the four reference docs from
`src/proj/docs.py` — the single source.

Run `proj --root <sandbox> docs` in a throwaway temp project, then copy the two
generated files to all four destinations (the templates/ seed copies are
byte-identical to the repo docs/):

    docs/CLI.md              <- sandbox/docs/CLI.md
    docs/MCP.md              <- sandbox/docs/MCP.md
    templates/PROJ_CLI.md   <- sandbox/docs/CLI.md   (proj init's seed copy)
    templates/PROJ_MCP.md   <- sandbox/docs/MCP.md
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    sb = Path(tempfile.mkdtemp(prefix="proj-docs-"))
    try:
        env = dict(os.environ)
        env.setdefault("PROJ_AGENT", "docs")
        for args in (["init", "--profile", "default"], ["docs"]):
            p = subprocess.run(["proj", *args], check=False,
                               capture_output=True, text=True,
                               env=env, cwd=str(sb))
            if p.returncode != 0:
                raise SystemExit(f"proj {' '.join(args)} failed:\n"
                                 f"{p.stdout}\n{p.stderr}")
        dests = [(sb / "docs" / "CLI.md", REPO / "docs" / "CLI.md"),
                 (sb / "docs" / "MCP.md", REPO / "docs" / "MCP.md"),
                 (sb / "docs" / "CLI.md", REPO / "templates" / "PROJ_CLI.md"),
                 (sb / "docs" / "MCP.md", REPO / "templates" / "PROJ_MCP.md")]
        # P0-4: the four dests are COMMITTED repo docs — refuse to overwrite
        # them from a missing or empty generation (a silent `proj docs`
        # regression would otherwise destroy the committed text with no error)
        for src, dst in dests:
            if not src.is_file() or not src.read_text(encoding="utf-8").strip():
                raise SystemExit(f"generated doc missing or empty: {src} — "
                                f"not touching committed {dst.name}")
        for src, dst in dests:
            shutil.copyfile(str(src), str(dst))
            print(f"wrote {dst.relative_to(REPO)}")
    finally:
        shutil.rmtree(sb, ignore_errors=True)


if __name__ == "__main__":
    main()
