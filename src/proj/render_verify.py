"""Incremental-vs-full render verification (F6.2, hard-parts-impl).

`verify(cfg)` renders EVERY record and view in memory (the same pure functions
`store.render_all` uses — no writes) and compares each result with the file on
disk, file by file. An empty result means the disk is fully in sync with the DB.

Engine hook: `store.render_affected` runs it after every incremental render when
`PROJ_RENDER_VERIFY=1` (production: unset → off, no extra full-render cost).
Tests and e2e turn it on by default (tests/conftest.py, scripts/e2e.py).
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
            elif (got := p.read_text(encoding="utf-8")) != want:
                g, w = got.splitlines(), want.splitlines()
                first = next((i + 1 for i in range(min(len(g), len(w)))
                              if g[i] != w[i]), min(len(g), len(w)) + 1)
                diffs.append((rel, "differs",
                              f"disk != full render (first difference at line {first})"))
        for p in sorted((cfg.root / "WIP.md", cfg.root / "CHANGELOG.md")):
            if p.exists() and str(p.relative_to(cfg.root)) not in expected:
                diffs.append((str(p.relative_to(cfg.root)),
                              "stale-disk", "file on disk, no render produces it"))
        return diffs
    finally:
        db.close()


def assert_in_sync(cfg: ProjConfig) -> None:
    """Raise (with every differing file named) when disk != a full render."""
    diffs = verify(cfg)
    if diffs:
        detail = "\n".join(f"  {rel}: {kind} — {d}" for rel, kind, d in diffs)
        raise RuntimeError(
            "PROJ_RENDER_VERIFY: incremental render left the disk out of sync "
            "with a full render:\n" + detail)
