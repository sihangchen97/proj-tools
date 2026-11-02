"""Render-vs-disk verification (PLAN §28.1 23:50, for F6's PROJ_RENDER_VERIFY).

`verify(cfg)` renders EVERY record and view in memory (the same pure functions
`store.render_all` uses — no writes) and compares each result with the file on
disk, file by file. It returns the list of diffs; an empty list means the disk
is fully in sync with the DB.

Switch: conftest runs it after every test only when `PROJ_RENDER_VERIFY=1`
(default OFF — F6 lands the default-ON; alex opens it).
"""
from __future__ import annotations

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.engine import store as rec
from proj.engine.views import render_changelog_md
from proj.engine.wip import render_wip_md


def verify(cfg: ProjConfig) -> list[tuple[str, str, str]]:
    """Full in-memory render vs disk. Returns [(rel_path, kind, detail), ...].

    kind is "differs" (disk has different text), "missing" (rendered content,
    no file on disk), or "stale-disk" (file on disk that no render produces).
    """
    reg = rec.load_registry(cfg.root)
    db = dbmod.Db(cfg.db_path)
    try:
        task_rows = db.all(reg["task"]) if "task" in reg else []
        expected: dict[str, str] = {}
        for spec in reg.values():
            for row in db.all(spec):
                p = rec.file_path(cfg, spec, row)
                rel = str(p.relative_to(cfg.root))
                expected[rel] = rec.render_record(spec, row, db, reg,
                                                  task_rows=task_rows)
        for view in (render_wip_md(cfg, db, reg),
                     render_changelog_md(cfg, db)):
            if view is None:
                continue
            rel = str(view.path.relative_to(cfg.root))
            expected[rel] = view.text

        diffs: list[tuple[str, str, str]] = []
        for rel, want in sorted(expected.items()):
            p = cfg.root / rel
            if not p.exists():
                diffs.append((rel, "missing", "rendered but no file on disk"))
            elif p.read_text(encoding="utf-8") != want:
                diffs.append((rel, "differs", "disk text != rendered text"))
        for p in sorted((cfg.root / "WIP.md", cfg.root / "CHANGELOG.md")):
            if p.exists() and str(p.relative_to(cfg.root)) not in expected:
                diffs.append((str(p.relative_to(cfg.root)),
                              "stale-disk", "file on disk, no render produces it"))
        return diffs
    finally:
        db.close()
