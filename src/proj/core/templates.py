"""RecordType registry from YAML profiles (v3: profile = schema, DB = storage).

A profile file declares a record_type's table schema:

    record_type: decision
    dir: docs/decisions
    numbered: 4          # 4 = NNNN, "letter" = A..Z/AA.., 0 = none
    status-allowed:      # each entry: {name: X, emoji: "…"} (emoji optional)
      - {name: Proposed, emoji: 🌱}
      - {name: Accepted, emoji: ✅}
    fields:              # record_type-specific extra columns (optional)
      - {name: supersedes, hint: ...}
    sections:            # body headings, in render order
      - {name: Context, hint: ...}
      - {name: Task List, view: tasks}   # view sections: engine-rendered, DB wins

Placeholders in rendered managed lines: none — the engine fills values
directly from the DB row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ChangelogSpec:
    """Global changelog config (profiles/changelog.yaml, not a record_type)."""
    file: str = "CHANGELOG.md"
    unreleased: str = "Unreleased"
    default_category: str = "Changed"
    categories: list[str] = field(default_factory=lambda: [
        "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"])


@dataclass
class TemplateField:
    name: str
    hint: str = ""


@dataclass
class TemplateSection:
    name: str
    hint: str = ""
    kind: str = "text"  # section type: "text" (user-owned) or "task" (rendered task
                        # subtree: tasks with parent == this record, recursively)


@dataclass
class RecordTypeSpec:
    record_type: str
    dir: str
    numbered: int | str = 4   # int (NNNN width) | "letter" | 0 (none)
    status_allowed: list[str] = field(default_factory=list)
    status_emoji: dict[str, str] = field(default_factory=dict)  # status name -> emoji
    verbs: dict[str, tuple[str, str]] = field(default_factory=dict)  # verb -> (from, to)
    fields: list[TemplateField] = field(default_factory=list)
    sections: list[TemplateSection] = field(default_factory=list)
    done_status: list[str] = field(default_factory=list)  # statuses that count as "done"

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def emojify(self, status: str) -> str:
        """Emoji only (table display): the emoji from this record_type's emoji
        mapping when the status is mapped, else the plain status.
        Comparison is slug + lowercase ('In progress' == 'in-progress').
        Handles {id}-pattern statuses (e.g. 'Superseded by DEC-0003')."""
        for name, emoji in self.status_emoji.items():
            if "{id}" in name:
                rx = re.escape(name).replace(re.escape("{id}"), r"\S+")
                if re.fullmatch(rx, status, re.IGNORECASE):
                    return emoji
                continue
            if slugify(name) == slugify(status):
                return emoji
        return status

    def status_display(self, status: str) -> str:
        """'🟢 Accepted' (emoji + name) for non-table display; plain if unmapped."""
        e = self.emojify(status)
        return f"{e} {status}" if e != status else status


def parse_template(text: str) -> RecordTypeSpec:
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "record_type" not in data:
        raise ValueError(f"template missing 'record_type' key: {data!r:.80}")
    numbered = data.get("numbered", 4)
    if numbered != "letter" and not isinstance(numbered, int):
        raise ValueError(
            f"record_type {data['record_type']!r}: numbered must be int (width) or 'letter'")
    # status entries: plain string, or {name: X, emoji: "🟢"} (emoji optional);
    # {name: X, emoji: ""} explicitly means "no emoji"; per-status remarks belong
    # in YAML comments, not in the data
    status_allowed: list[str] = []
    status_emoji: dict[str, str] = {}
    for s in data.get("status-allowed", []):
        if isinstance(s, dict):
            status_allowed.append(str(s["name"]))
            if s.get("emoji"):
                status_emoji[str(s["name"])] = str(s["emoji"])
        else:
            status_allowed.append(str(s))
    # verbs: {verb: {from: X, to: Y}} — state machine steps as template data
    verbs: dict[str, tuple[str, str]] = {}
    for verb, v in data.get("verbs", {}).items():
        verbs[str(verb)] = (str(v["from"]), str(v["to"]))
    spec = RecordTypeSpec(
        record_type=data["record_type"],
        dir=data.get("dir", f"docs/{data['record_type']}s").rstrip("/"),
        numbered=numbered,
        status_allowed=status_allowed,
        status_emoji=status_emoji,
        verbs=verbs,
        fields=[TemplateField(f["name"], f.get("hint", "")) for f in data.get("fields", [])],
        sections=[TemplateSection(s["name"], s.get("hint", ""), str(s.get("type", "text")))
                  for s in data.get("sections", [])],
        done_status=[str(s) for s in data.get("done-status", [])],
    )
    unknown = [s for s in spec.done_status if s not in spec.status_allowed]
    if unknown:
        raise ValueError(f"template {spec.record_type!r}: done-status "
                         f"{unknown} not in status-allowed")
    return spec


def bundled_dir() -> Path:
    # bundled profiles live in the repo root profiles/ (moved out of src/proj/);
    # installed wheels may carry a copy inside the package instead
    repo = Path(__file__).resolve().parents[3] / "profiles"
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parents[1] / "profiles"


def _scan(base: Path) -> dict[str, RecordTypeSpec]:
    """Record_type profiles from base-level yamls and subdirectories.

    A base-level file without a `record_type` key (e.g. changelog.yaml) is
    global config — skipped. A file *with* a record_type key must parse
    cleanly; broken profiles are never swallowed silently."""
    out: dict[str, RecordTypeSpec] = {}
    files = sorted(base.glob("*.yaml"))
    files += [f for d in sorted(p for p in base.iterdir() if p.is_dir())
              for f in sorted(d.glob("*.yaml"))]
    for f in files:
        text = f.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not (isinstance(data, dict) and "record_type" in data):
            continue
        spec = parse_template(text)
        out[spec.record_type] = spec
    return out


def load_registry(project_root: Path | None = None) -> dict[str, RecordTypeSpec]:
    """RecordType registry: bundled profiles as the base, the project's own
    profiles/ overrides them per record_type (the profiles/ dir may exist with
    only changelog.yaml and still keep every bundled record_type)."""
    registry = _scan(bundled_dir())
    project = project_root / "profiles" if project_root else None
    if project is not None and project.is_dir():
        registry.update(_scan(project))
    return registry


def changelog_path(root: Path | None) -> Path:
    """profiles/changelog.yaml: the project's copy wins, else the bundled one."""
    if root is not None:
        p = root / "profiles" / "changelog.yaml"
        if p.is_file():
            return p
    return bundled_dir() / "changelog.yaml"


def load_changelog_spec(root: Path | None = None) -> ChangelogSpec:
    data = yaml.safe_load(changelog_path(root).read_text(encoding="utf-8"))
    cl = data.get("changelog", {})
    return ChangelogSpec(
        file=cl.get("file", "CHANGELOG.md"),
        unreleased=cl.get("unreleased", "Unreleased"),
        default_category=cl.get("default_category", "Changed"),
        categories=list(cl.get("categories", [])) or ChangelogSpec().categories,
    )


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
