"""`proj changelog` — global CHANGELOG.md, rendered from the DB (v3).

Config (categories, unreleased placeholder, output file) comes from
profiles/changelog.yaml, not hard-coded here.
"""
from __future__ import annotations

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.core.templates import load_changelog_spec
from proj.engine import store as rec
from proj.engine.views import read_versions, release, render_changelog_md


def add(cfg: ProjConfig, entry: str, version: str | None = None) -> None:
    cl = load_changelog_spec(cfg.root)
    version = version or cl.unreleased
    category, text = dbmod.parse_summary(entry, cl)
    db = rec.open_db(cfg)
    db.add_cl_entry(version, category, text)
    db.close()
    _write(cfg)


def _write(cfg: ProjConfig) -> None:
    db = rec.open_db(cfg)
    view = render_changelog_md(cfg, db)
    db.close()
    view.path.write_text(view.text, encoding="utf-8")


def version_bump(cfg: ProjConfig, version: str | None, bump_level: str | None):
    db = rec.open_db(cfg)
    used = release(cfg, db, version=version, bump_level=bump_level)
    db.close()
    _write(cfg)
    return used


def release_notes(cfg: ProjConfig, version: str) -> str:
    cl = load_changelog_spec(cfg.root)
    db = rec.open_db(cfg)
    key = version.lstrip("v")
    for candidate in (version, key, "v" + key):
        rows = db.cl_entries(candidate)
        if rows:
            db.close()
            lines = [f"# {candidate}", ""]
            by_cat: dict[str, list[str]] = {}
            for e in rows:
                by_cat.setdefault(e["category"], []).append(e["text"])
            for cat in cl.categories:
                if cat in by_cat:
                    lines += [f"### {cat}", ""]
                    lines += [f"- {t}" for t in by_cat[cat]]
                    lines.append("")
            return "\n".join(lines).rstrip() + "\n"
    db.close()
    raise ValueError(f"no changelog entries for {version!r}")


def show_versions(cfg: ProjConfig) -> list[tuple[str, str]]:
    db = rec.open_db(cfg)
    try:
        return read_versions(cfg, db)
    finally:
        db.close()
