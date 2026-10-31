"""`proj init` — scaffold, lock, and adoption (v3). Idempotent."""
from __future__ import annotations

import json
from pathlib import Path

from proj.core import db as dbmod
from proj.core.config import ProjConfig
from proj.core.templates import load_registry, load_texts, normalize_profiles
from proj.engine import store as rec

PROFILE_DIRS = {
    "default": [],
    "cv": ["docs/experiments"],
    "infra": ["docs/specs", "docs/dev"],
    "quality": ["docs/tech-debt", "docs/incidents"],
}

# scaffold templates are real files in the repo's templates/ (bundled with the
# wheel as proj/templates): copy them over at init instead of keeping their
# text inside this module.

def templates_dir() -> Path:
    # repo root (src/proj/scaffold.py → two levels up) carries templates/;
    # installed wheels carry a copy inside the package instead
    repo = Path(__file__).resolve().parents[2] / "templates"
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parents[0] / "templates"


def init(cfg: ProjConfig, profiles: list[str], zoo: bool = False) -> list[str]:
    """Create missing governance files (copy from templates/), build tables
    from lock or templates, seed decision 0001, and adopt pre-existing files.
    zoo=True also drops the agent rule file (.roo/rules/proj-tools.md)."""
    created: list[str] = []

    texts = load_texts(cfg.root)  # scaffold + seed content (profiles/texts.yaml)

    def add(rel: str, content: str) -> None:
        p = cfg.root / rel
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        created.append(rel)

    # lock pins the profile dirs used to build the registry. Repeat inits with
    # extra profiles append to the pinned list (default first); only then is the
    # registry rebuilt from templates and the lock rewritten (expanded).
    lock = dbmod.read_lock(cfg.root)
    pinned = normalize_profiles((list(lock.get("profiles", [])) if lock else [])
                                + list(profiles))
    if lock is None or pinned != list(lock.get("profiles", [])):
        specs = load_registry(cfg.root, profiles=pinned)
    else:
        specs = dbmod.specs_from_lock(lock)

    # dirs
    dirs = [s.dir for s in specs.values()]
    for prof in profiles:
        dirs.extend(PROFILE_DIRS.get(prof, []))
    for d in dirs:
        p = cfg.root / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            created.append(d + "/")

    # nothing from profiles/ is copied into the project: the registry (and the
    # changelog config) fall back to the bundled profiles (repo root or
    # site-packages); the lock records the specs that were actually used. A
    # project that wants to override a record_type or the changelog adds its
    # own profiles/<name>.yaml — those override the bundled ones.

    add("CHANGELOG.md", "")      # rendered from DB on first update
    add("TASKLIST.md", "")       # rendered from DB on first update

    # real files copied from templates/ (user-editable afterwards; never
    # re-created once present)
    def copy_tpl(src: str, dst: str) -> None:
        p = cfg.root / dst
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((templates_dir() / src).read_text(encoding="utf-8"),
                     encoding="utf-8")
        created.append(dst)

    copy_tpl("env.example", ".env.example")
    copy_tpl("CONTRIBUTING.md", "CONTRIBUTING.md")
    # D6: the project .env is committed like the DB (clone = same defaults);
    # an empty PROJ_LOGS line documents the available config var
    add(".env", "# project config defaults (D6): OS env > .env > code default\n"
                "# PROJ_LOGS=\n")
    if zoo:
        copy_tpl("zoo-rule.md", ".roo/rules/proj-tools.md")

    db = rec.open_db(cfg)
    if lock is None or pinned != list(lock.get("profiles", [])):
        dbmod.write_lock(cfg.root, specs, pinned)
        created.append(dbmod.LOCK_FILE)
    for spec in specs.values():
        db.ensure_kind(spec)
    db.close()

    # seed decision 0001 only if none exist yet
    if "decision" in specs:
        spec = specs["decision"]
        if not rec.open_db(cfg).all(spec) and not any(
                x for x in (cfg.root / spec.dir).glob("*.md")
                if not x.name.startswith("_")):
            seed = texts["scaffold"]["seed_decision"]
            row, p = rec.new_record(cfg, "decision", seed["title"],
                                     summary=seed.get("summary", ""), spec=spec)
            rec.status_set(cfg, "decision", row["id"], seed["status"])
            d = rec.open_db(cfg)
            d.set(spec, row["id"], sections=json.dumps(seed["sections"]))
            fresh = d.get(spec, row["id"])
            p.write_text(rec.render_record(spec, fresh, d), encoding="utf-8")
            d.close()
            created.append(str(p.relative_to(cfg.root)))

    # adopt any pre-existing managed files, then render views
    problems, notes = rec.adopt(cfg)
    for n in notes:
        created.append(f"adopted: {n}")
    if problems:
        raise ValueError("adoption failed:\n" + "\n".join(problems))
    return created
