"""`proj changelog` — global CHANGELOG.md, rendered from the DB.

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
    rec.ensure_synced(cfg)  # A1 (uniform write-command entry)
    version = version or cl.unreleased
    category, text = dbmod.parse_summary(entry, cl)
    # R3: one write transaction for the changelog entry
    with rec.open_db(cfg) as db:
        db.begin()
        db.add_cl_entry(version, category, text)
    _write(cfg)


def _write(cfg: ProjConfig) -> None:
    with rec.open_db(cfg) as db:  # R3: read-only
        view = render_changelog_md(cfg, db)
    view.path.write_text(view.text, encoding="utf-8")


def version_bump(cfg: ProjConfig, version: str | None, bump_level: str | None):
    # R3: the whole release (Unreleased → the version) is ONE transaction
    with rec.open_db(cfg) as db:
        db.begin()
        used = release(cfg, db, version=version, bump_level=bump_level)
    _write(cfg)
    return used


def release_notes(cfg: ProjConfig, version: str) -> str:
    cl = load_changelog_spec(cfg.root)
    key = version.lstrip("v")
    with rec.open_db(cfg) as db:  # R3: read-only
        for candidate in (version, key, "v" + key):
            rows = db.cl_entries(candidate)
            if rows:
                lines = [f"# {candidate}", ""]
                by_cat: dict[str, list[str]] = {}
                for e in rows:
                    by_cat.setdefault(e["category"], []).append(e["message"])
                for cat in cl.categories:
                    if cat in by_cat:
                        lines += [f"### {cat}", ""]
                        lines += [f"- {t}" for t in by_cat[cat]]
                        lines.append("")
                return "\n".join(lines).rstrip() + "\n"
    raise ValueError(f"no changelog entries for {version!r}")


def show_versions(cfg: ProjConfig) -> list[tuple[str, str]]:
    with rec.open_db(cfg) as db:  # R3: read-only
        return read_versions(cfg, db)
