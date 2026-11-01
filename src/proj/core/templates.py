"""RecordType registry from YAML profiles (profile = schema, DB = storage).

A profile file declares a record_type's table schema and behavior:

    record_type: decision
    dir: docs/decisions
    numbered: 4          # 4 = NNNN (digits width); must be an int > 0 (D9:
    # the id prefix {code}-{digits} is how relations rows resolve a record's type)
    code: DEC            # 3-letter record code; used in refs (DEC-0001) and the graph
    verbs:               # one verb per status: name -> {from, to}; from: Any = any state
      accept: {from: Any, to: Accepted}
      deprecate: {from: Any, to: Deprecated}
    status-allowed:      # each entry: {name, emoji, done} (emoji/done optional)
      - {name: Proposed, emoji: 🌱}
      - {name: Accepted, emoji: ✅, done: true}   # done: true => counts as the "done" state
    contain_task: true     # tasks attach (parent -> this): head task tree, subtask row
    sections:            # body headings, in render order
      - {name: Context, hint: ...}
      - {name: Task List, type: task}   # view section: engine-rendered task tree
    # render toggles — which generated views this record_type has
    # (the per-record changelog table's row count is the PROJ_LOGS env var)
    render:
      toc: false            # <dir>/_TOC.md (id/status/title table)

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
class TemplateSection:
    name: str
    hint: str = ""
    kind: str = "text"  # section type: "text" (user-owned) or "task" (rendered task
                        # subtree: tasks with parent == this record, recursively)


@dataclass
class RecordTypeSpec:
    record_type: str
    dir: str
    numbered: int = 4   # int (NNNN width), must be > 0 (D9)
    status_allowed: list[str] = field(default_factory=list)
    status_emoji: dict[str, str] = field(default_factory=dict)  # status name -> emoji
    verbs: dict[str, tuple[object, str]] = field(default_factory=dict)  # verb -> (from, to)
    contain_task: bool = False  # tasks attach (parent -> this): head task tree, subtask row
    sections: list[TemplateSection] = field(default_factory=list)
    code: str = ""  # 3-letter record code (e.g. DEC); used in refs (DEC-0001) and the graph
    status_done: list[str] = field(default_factory=list)  # statuses declared done: true
    # render toggles (which generated views this record_type has)
    apply_toc: bool = False        # <dir>/_TOC.md (id/status/title table)

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
    if not isinstance(numbered, int):
        raise ValueError(
            f"record_type {data['record_type']!r}: numbered must be an int (id width)")
    # status entries: plain string, or {name: X, emoji: "🟢"} (emoji optional);
    # {name: X, emoji: ""} explicitly means "no emoji"; per-status remarks belong
    # in YAML comments, not in the data
    status_allowed: list[str] = []
    status_emoji: dict[str, str] = {}
    status_done: list[str] = []
    for s in data.get("status-allowed", []):
        if isinstance(s, dict):
            status_allowed.append(str(s["name"]))
            if s.get("emoji"):
                status_emoji[str(s["name"])] = str(s["emoji"])
            if s.get("done"):
                status_done.append(str(s["name"]))
        else:
            status_allowed.append(str(s))
    # verbs: {verb: {from: X, to: Y}} — state machine steps as template data;
    # from: "Any" | a single status | a list of statuses
    verbs: dict[str, tuple[object, str]] = {}
    for verb, v in data.get("verbs", {}).items():
        frm = v["from"]
        if str(frm).upper() == "ANY":
            frm = "Any"
        elif isinstance(frm, list):
            frm = [str(s) for s in frm]
        else:
            frm = str(frm)
        verbs[str(verb)] = (frm, str(v["to"]))
    spec = RecordTypeSpec(
        record_type=data["record_type"],
        dir=data.get("dir", f"docs/{data['record_type']}s").rstrip("/"),
        numbered=numbered,
        status_allowed=status_allowed,
        status_emoji=status_emoji,
        verbs=verbs,
        contain_task=bool(data.get("contain_task", False)),
        sections=[TemplateSection(s["name"], s.get("hint", ""), str(s.get("type", "text")))
                  for s in data.get("sections", [])],
        code=str(data.get("code", "")),
        status_done=status_done,
        apply_toc=bool(data.get("render", {}).get("toc", False)),
    )
    unknown = [s for s in spec.status_done if s not in spec.status_allowed]
    if unknown:
        msg = f"template {spec.record_type!r}: status done {unknown} not in status-allowed"
        raise ValueError(msg)
    # id layout: {code}-{N digits} = 7 chars total (DEC-0001, DEB-0001, T-000001)
    # D9: numbered must be an int > 0 — every record id then carries a
    # resolvable {code}-{digits} prefix (relations FK rows resolve type from it)
    if not (isinstance(spec.numbered, int) and spec.numbered > 0):
        raise ValueError(
            f"template {spec.record_type!r}: numbered must be an int > 0 (id width)")
    if not spec.code.isupper() or not spec.code.isalpha() or not (1 <= len(spec.code) <= 3):
        raise ValueError(
            f"template {spec.record_type!r}: code must be 1-3 uppercase letters "
            f"(got {spec.code!r})")
    if len(spec.code) + spec.numbered != 7:
        raise ValueError(
            f"template {spec.record_type!r}: code + numbered must total 7 "
            f"(got {len(spec.code)}+{spec.numbered})")
    return spec


def bundled_dir() -> Path:
    # bundled profiles live in the repo root profiles/ (moved out of src/proj/);
    # installed wheels may carry a copy inside the package instead
    repo = Path(__file__).resolve().parents[3] / "profiles"
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parents[1] / "profiles"


def default_profile_dirs(project_root: Path | None) -> list[str]:
    """Selected profile dirs: the profiles list pinned in the lock (always
    'default' first), or ['default'] before init."""
    lock = None
    if project_root is not None:
        path = project_root / ".proj.lock"
        if path.is_file():
            lock = yaml.safe_load(path.read_text(encoding="utf-8"))
    pinned = list(lock.get("profiles", []) if isinstance(lock, dict) else [])
    if "default" not in pinned:
        pinned.insert(0, "default")
    return pinned


def normalize_profiles(profiles: list[str]) -> list[str]:
    """'default' always first; extras appended, deduplicated."""
    out = ["default"]
    for p in profiles:
        if p != "default" and p not in out:
            out.append(p)
    return out


def _scan(base: Path, profile_dirs: list[str] | None = None) -> dict[str, RecordTypeSpec]:
    """Record_type profiles: base-level yamls always + the *selected* profile
    subdirectories (default_profile_dirs / normalize_profiles decide which).

    A base-level file without a `record_type` key (e.g. changelog.yaml) is
    global config — skipped. A file *with* a record_type key must parse
    cleanly; broken profiles are never swallowed silently."""
    out: dict[str, RecordTypeSpec] = {}
    files = sorted(base.glob("*.yaml"))
    files += [f for d in (base / p for p in profile_dirs) if d.is_dir()
              for f in sorted(d.glob("*.yaml"))]
    for f in files:
        text = f.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not (isinstance(data, dict) and "record_type" in data):
            continue
        spec = parse_template(text)
        out[spec.record_type] = spec
    return out


def load_registry(project_root: Path | None = None,
                  profiles: list[str] | None = None) -> dict[str, RecordTypeSpec]:
    """RecordType registry: bundled profiles as the base, the project's own
    profiles/ overrides them per record_type. Only the *selected* profile
    subdirectories are scanned (the lock-pinned list, 'default' first, or the
    explicit `profiles` when given — init passes it before the lock exists) —
    unselected profile dirs are inert."""
    dirs = normalize_profiles(profiles) if profiles else default_profile_dirs(project_root)
    registry = _scan(bundled_dir(), dirs)
    project = project_root / "profiles" if project_root else None
    if project is not None and project.is_dir():
        registry.update(_scan(project, dirs))
    return registry


def resolve_record_type(name: str, registry: dict[str, RecordTypeSpec] | None = None) -> str:
    """Canonical record_type name. Case-insensitive; accepts the record_type name
    OR the code (e.g. 'decision'/'DECISION'/'dec'/'DEC' → 'decision', 'T' → 'task')."""
    reg = registry if registry is not None else load_registry()
    name = (name or "").strip()
    if name in reg:
        return name
    low = name.lower()
    if low in reg:
        return low
    for rt, spec in reg.items():
        if spec.code and spec.code.lower() == low:
            return rt
    raise ValueError(f"unknown record_type {name!r} "
                     f"(registered: {', '.join(sorted(reg))})")


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


def load_texts(root: Path | None = None) -> dict:
    """profiles/texts.yaml: the human-facing text the tool writes into project
    files. A project's own copy wins (wholesale), else the bundled one."""
    texts = (root / "profiles" / "texts.yaml") if root is not None else None
    if texts is not None and texts.is_file():
        data = yaml.safe_load(texts.read_text(encoding="utf-8"))
    else:
        data = yaml.safe_load((bundled_dir() / "texts.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("texts.yaml: expected a mapping at the top level")
    return data


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
