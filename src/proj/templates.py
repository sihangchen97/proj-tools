"""Kind registry from YAML templates (v3: template = schema, DB = storage).

A template file declares a record kind's table schema:

    kind: decision
    dir: docs/decisions
    numbered: 4          # 4 = NNNN, "letter" = A..Z/AA.., "date" = YYYY-MM-DD, 0 = none
    status-allowed: [Proposed, Accepted, ...]
    fields:              # kind-specific extra columns (optional)
      - {name: supersedes, hint: ...}
    sections:            # body headings, in render order
      - {name: Context, hint: ...}
      - {name: Task List, view: tasks}   # view sections: engine-rendered, DB wins

Placeholders in rendered managed lines: none — the engine fills values
directly from the DB row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class TemplateField:
    name: str
    hint: str = ""


@dataclass
class TemplateSection:
    name: str
    hint: str = ""
    view: str = ""      # view renderer name (e.g. "tasks"); empty = user-owned


@dataclass
class TemplateDerived:
    name: str           # managed line label, e.g. "Started"
    to_status: str      # first changelog entry with this `to` provides the date


@dataclass
class KindSpec:
    kind: str
    dir: str
    numbered: object = 4   # int | "letter" | "date"
    status_allowed: list[str] = field(default_factory=list)
    fields: list[TemplateField] = field(default_factory=list)
    sections: list[TemplateSection] = field(default_factory=list)
    derived: list[TemplateDerived] = field(default_factory=list)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    @property
    def section_names(self) -> list[str]:
        return [s.name for s in self.sections]

    def view_sections(self) -> list[TemplateSection]:
        return [s for s in self.sections if s.view]


def parse_template(text: str) -> KindSpec:
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "kind" not in data:
        raise ValueError(f"template missing 'kind' key: {data!r:.80}")
    numbered = data.get("numbered", 4)
    if numbered not in ("letter", "date") and not isinstance(numbered, int):
        raise ValueError(f"kind {data['kind']!r}: numbered must be int|letter|date")
    return KindSpec(
        kind=data["kind"],
        dir=data.get("dir", f"docs/{data['kind']}s").rstrip("/"),
        numbered=numbered,
        status_allowed=[str(s) for s in data.get("status-allowed", [])],
        fields=[TemplateField(f["name"], f.get("hint", "")) for f in data.get("fields", [])],
        sections=[TemplateSection(s["name"], s.get("hint", ""), s.get("view", ""))
                  for s in data.get("sections", [])],
        derived=[TemplateDerived(d["name"], d["to-status"]) for d in data.get("derived", [])],
    )


def bundled_dir() -> Path:
    return Path(__file__).parent / "templates"


def load_registry(project_root: Path | None = None) -> dict[str, KindSpec]:
    """Kind registry from project templates/ (falls back to bundled defaults)."""
    base = (project_root / "templates") if project_root else None
    if base is None or not base.is_dir():
        base = bundled_dir()
    registry: dict[str, KindSpec] = {}
    for profile in sorted(p for p in base.iterdir() if p.is_dir()):
        for f in sorted(profile.glob("*.yaml")):
            spec = parse_template(f.read_text(encoding="utf-8"))
            registry[spec.kind] = spec  # later profiles override earlier ones
    return registry


def slugify(title: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
