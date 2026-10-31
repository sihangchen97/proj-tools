"""Git hooks: pre-commit runs `proj check`. Installs into .git/hooks/.

The hook embeds the *absolute* path of the proj binary resolved at install
time — a bare `proj` on PATH may be an unrelated program (name clash),
which would run the wrong binary on every commit.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from proj.core.config import ProjConfig

PRE_COMMIT = """\
#!/bin/sh
# Installed by `proj hooks install` — runs governance checks on every commit.
set -e
if [ -x "{proj}" ]; then
    exec "{proj}" check
fi
echo "proj: hook target not found, skipping governance check"
"""


def _find_proj(root: Path) -> str:
    """Absolute path to the proj binary: the project's pixi dev env wins
    (this repo builds proj itself), then the first `proj` on PATH."""
    candidates = [root / ".pixi" / "envs" / "dev" / "bin" / "proj",
                  Path(shutil.which("proj") or "proj")]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    return str(candidates[0])


def install(cfg: ProjConfig) -> str:
    hooks_dir = cfg.root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise ValueError("no .git in this project — `git init` first")
    hook = hooks_dir / "pre-commit"
    if hook.exists() and "proj hooks install" not in hook.read_text(encoding="utf-8"):
        raise ValueError(f"{hook} is not a proj hook — refusing to overwrite")
    hook.write_text(PRE_COMMIT.format(proj=_find_proj(cfg.root)), encoding="utf-8")
    hook.chmod(0o755)
    return str(hook)
