"""RecordType registry from YAML profiles (profile = schema, DB = storage).

A profile file declares a record_type's table schema and behavior:

    record_type: decision
    dir: docs/decisions
    numbered: 4          # 4 = id-suffix width (digits only: 10**W values, e.g. 0001…9999).
    # int > 0 (D9). No letter overflow: when the digit space is used up, new ids raise.
    # The {code}- prefix is how relations rows resolve a record's type.
    code: DEC            # 3-letter record code; used in refs (DEC-0001) and the graph
    verbs:               # one verb per status: name -> {from, to}; from: Any = any state
      accept: {from: Any, to: Accepted}
      deprecate: {from: Any, to: Deprecated}
    status-allowed:      # each entry: {name, emoji, done, terminal} (emoji/done/terminal optional)
      - {name: Proposed, emoji: 🌱}
      - {name: Accepted, emoji: ✅, done: true}   # done: true => counts as the "done" state
      - {name: Failed, emoji: ❌, terminal: true} # terminal: true => counts as "finished" for gates
    gates:               # optional; statuses that require the check to pass
      tasks: [Done]      # entering these statuses: every descendant task must be in a
                         #   terminal task status (the task's own gates/tasks list decides)
      items: [Done]      # entering these statuses: every item must be closed (phase 2)
    sections:            # body headings, in render order
      - {name: Context, hint: ...}
      # no task section needed: records parenting tasks auto-render a Tasks block
    # render toggles — which generated views this record_type has
    # (the per-record changelog table's row count is the PROJ_LOGS env var)
    render:

Placeholders in rendered managed lines: none — the engine fills values
directly from the DB row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# libyaml's C loader when available (~12x faster than pure-Python SafeLoader),
# else the pure-Python fallback
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _yaml_load(text: str):
    return yaml.load(text, Loader=_YAML_LOADER)


# in-process cache for load_registry: profiles are parsed ~65x per test session;
# the key covers every yaml file scanned + the lock (mtime/size), so edits to
# a profile invalidate the entry automatically. spec objects are shared and
# treated read-only by every caller (only the returned dict may be mutated).
_REGISTRY_CACHE: dict[tuple, dict[str, RecordTypeSpec]] = {}


@dataclass
class ChangelogSpec:
    """Global changelog config (profiles/changelog.yaml, not a record_type)."""
    file: str = "CHANGELOG.md"
    unreleased: str = "Unreleased"
    default_category: str = "Changed"
    categories: list[str] = field(default_factory=lambda: [
        "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"])


@dataclass
class ItemState:
    """One state of a record's item checklist: `name` (required), optional
    `emoji` (shown before the text when its group holds more than one state),
    `closed` (group: todo=False, done=True)."""

    name: str
    emoji: str = ""
    closed: bool = False


@dataclass
class ItemsSpec:
    """The `items:` block: a record's structured checklist (one `##` section)."""

    section: str
    noun: str = "item"
    states: list[ItemState] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)   # subset of {owner, due}
    promote_from: list[str] = field(default_factory=list)
    promote_to: str = ""

    @property
    def closed_states(self) -> list[str]:
        """The names of the closed states (the 'done' half of the checklist)."""
        return [s.name for s in self.states if s.closed]


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
    numbered: int = 4   # int (id-suffix width: digits only, no letter overflow), must be > 0 (D9)
    status_allowed: list[str] = field(default_factory=list)
    status_emoji: dict[str, str] = field(default_factory=dict)  # status name -> emoji
    verbs: dict[str, tuple[object, str]] = field(default_factory=dict)  # verb -> (from, to)
    sections: list[TemplateSection] = field(default_factory=list)
    code: str = ""  # 3-letter record code (e.g. DEC); used in refs (DEC-0001) and the graph
    status_done: list[str] = field(default_factory=list)  # statuses declared done: true
    # "finished" statuses: done: true + terminal: true (e.g. Failed) — a task in
    # one of these never blocks its ancestors' gates
    status_terminal: list[str] = field(default_factory=list)
    # entering gates_tasks: all descendant tasks must be terminal; entering
    # gates_items: all items must be closed (items come in phase 2)
    gates_tasks: list[str] = field(default_factory=list)
    gates_items: list[str] = field(default_factory=list)
    items: ItemsSpec | None = None
    # WIP (plans §5): statuses that, when a record SWITCHES INTO them, add it to
    # WIP.md automatically. Empty = manual `proj wip add` only.
    wip_enter: list[str] = field(default_factory=list)
    # (generated views are derived; no per-type render toggles)

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
    data = _yaml_load(text)
    if not isinstance(data, dict) or "record_type" not in data:
        raise ValueError(f"template missing 'record_type' key: {data!r:.80}")
    if "dir" not in data:
        raise ValueError(f"template {data['record_type']!r}: dir is required")
    numbered = data.get("numbered", 4)
    if not isinstance(numbered, int):
        raise ValueError(
            f"record_type {data['record_type']!r}: numbered must be an int (id-suffix width)")
    # status entries: plain string, or {name: X, emoji: "🟢"} (emoji optional);
    # {name: X, emoji: ""} explicitly means "no emoji"; per-status remarks belong
    # in YAML comments, not in the data
    status_allowed: list[str] = []
    status_emoji: dict[str, str] = {}
    status_done: list[str] = []
    status_terminal: list[str] = []
    for s in data.get("status-allowed", []):
        if isinstance(s, dict):
            status_allowed.append(str(s["name"]))
            if s.get("emoji"):
                status_emoji[str(s["name"])] = str(s["emoji"])
            if s.get("done"):
                status_done.append(str(s["name"]))
            if s.get("terminal"):
                status_terminal.append(str(s["name"]))
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
    gates = data.get("gates") or {}
    items = _parse_items(data["record_type"], data.get("items"))
    spec = RecordTypeSpec(
        record_type=data["record_type"],
        dir=str(data["dir"]),
        numbered=numbered,
        status_allowed=status_allowed,
        status_emoji=status_emoji,
        verbs=verbs,
        sections=[TemplateSection(s["name"], s.get("hint", ""), str(s.get("type", "text")))
                  for s in data.get("sections", [])],
        code=str(data.get("code", "")),
        status_done=status_done,
        # done implies terminal (a done status always counts as finished);
        # terminal adds non-done finals (e.g. Failed)
        status_terminal=sorted(set(status_done) | set(status_terminal)),
        gates_tasks=[str(s) for s in gates.get("tasks", [])],
        gates_items=[str(s) for s in gates.get("items", [])],
        items=items,
        wip_enter=[str(s) for s in (data.get("wip") or {}).get("enter", [])],
    )
    unknown = [s for s in spec.status_done + spec.status_terminal if s not in spec.status_allowed]
    if unknown:
        msg = (f"template {spec.record_type!r}: status done/terminal {unknown} "
               f"not in status-allowed")
        raise ValueError(msg)
    for kind, listed in (("gates.tasks", spec.gates_tasks), ("gates.items", spec.gates_items)):
        bad = [s for s in listed if s not in spec.status_allowed]
        if bad:
            raise ValueError(
                f"template {spec.record_type!r}: {kind} {bad} not in status-allowed")
    # WIP (plans §5): wip.enter statuses must be allowed and never terminal
    # (a terminal status would immediately auto-return what it just auto-added)
    bad = [s for s in spec.wip_enter if s not in spec.status_allowed]
    if bad:
        raise ValueError(
            f"template {spec.record_type!r}: wip.enter {bad} not in status-allowed")
    bad = [s for s in spec.wip_enter if s in spec.status_terminal]
    if bad:
        raise ValueError(
            f"template {spec.record_type!r}: wip.enter {bad} must not be terminal")
    if spec.items is not None:
        _validate_items(spec)
    if spec.gates_items and spec.items is None:
        raise ValueError(
            f"template {spec.record_type!r}: gates.items declared but no items: block")
    # D9: numbered must be an int > 0 — every record id then carries a
    # resolvable {code}- prefix (relations FK rows resolve the type from the code,
    # the suffix after it is digits only — no letter overflow, see db.next_id)
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


def _parse_items(record_type: str, data) -> ItemsSpec | None:
    if data is None:
        return None
    if not isinstance(data, dict) or not data.get("section"):
        raise ValueError(f"template {record_type!r}: items: needs a section: key")
    states = [ItemState(str(s["name"]), str(s.get("emoji", "")), bool(s.get("closed", False)))
              if isinstance(s, dict) else ItemState(str(s))
              for s in data.get("states", [])]
    fields = [str(f) for f in data.get("fields", [])]
    promote = data.get("promote") or {}
    return ItemsSpec(
        section=str(data["section"]),
        noun=str(data.get("noun", "item")),
        states=states,
        fields=fields,
        promote_from=[str(s) for s in promote.get("from", [])],
        promote_to=str(promote.get("to", "")),
    )


def _validate_items(spec: RecordTypeSpec) -> None:
    """The items: rules (plan §2.1): known section, ≥2 states, first not closed,
    ≥1 closed, unique names, per-group emoji rule, known fields, promote statuses."""
    rt = spec.record_type
    it = spec.items
    if it is None:
        return
    if it.section not in [s.name for s in spec.sections]:
        raise ValueError(f"template {rt!r}: items.section {it.section!r} is not a section")
    if len(it.states) < 2:
        raise ValueError(f"template {rt!r}: items.states needs at least 2 states")
    if it.states[0].closed:
        raise ValueError(f"template {rt!r}: items.states first state must not be closed")
    if not any(s.closed for s in it.states):
        raise ValueError(f"template {rt!r}: items.states needs at least one closed state")
    names = [s.name for s in it.states]
    dup = [n for n in set(names) if names.count(n) > 1]
    if dup:
        raise ValueError(f"template {rt!r}: items.states names not unique: {dup}")
    for closed in (False, True):
        group = [s for s in it.states if s.closed is closed]
        if len(group) > 1:
            missing = [s.name for s in group if not s.emoji]
            if missing:
                raise ValueError(
                    f"template {rt!r}: items.states group needs unique emoji "
                    f"when it holds several states — missing for {missing}")
            emojis = [s.emoji for s in group if s.emoji]
            dup_e = [e for e in set(emojis) if emojis.count(e) > 1]
            if dup_e:
                raise ValueError(f"template {rt!r}: items.states emoji not unique: {dup_e}")
    bad_fields = [f for f in it.fields if f not in ("owner", "due")]
    if bad_fields:
        raise ValueError(f"template {rt!r}: items.fields only owner/due allowed (got {bad_fields})")
    bad = [s for s in it.promote_from + ([it.promote_to] if it.promote_to else [])
           if s not in spec.status_allowed]
    if bad:
        raise ValueError(f"template {rt!r}: items.promote {bad} not in status-allowed")


def bundled_dir() -> Path:
    # bundled profiles live in the repo root profiles/ (moved out of src/proj/);
    # installed wheels may carry a copy inside the package instead
    repo = Path(__file__).resolve().parents[3] / "profiles"
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parents[1] / "profiles"


# R4: the profiles list pinned in the lock, parsed at most once per (mtime, size) —
# computing a cache key must not re-parse the lock (it did ~1200x per e2e run).
_LOCK_PROFILES: dict[tuple, list[str]] = {}


def _lock_profiles(project_root: Path | None) -> list[str]:
    """Profiles pinned in `.proj.lock` (no lock → []). Cached by lock mtime/size;
    a lock rewrite changes the key, so a re-parse is always fresh."""
    if project_root is None:
        return []
    path = project_root / ".proj.lock"
    if not path.is_file():
        return []
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    prof = _LOCK_PROFILES.get(key)
    if prof is None:
        lock = _yaml_load(path.read_text(encoding="utf-8"))
        prof = list(lock.get("profiles", []) if isinstance(lock, dict) else [])
        _LOCK_PROFILES.clear()
        _LOCK_PROFILES[key] = prof
    return prof


def default_profile_dirs(project_root: Path | None) -> list[str]:
    """Selected profile dirs: the profiles list pinned in the lock (always
    'default' first), or ['default'] before init."""
    pinned = list(_lock_profiles(project_root))
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
        data = _yaml_load(text)
        if not (isinstance(data, dict) and "record_type" in data):
            continue
        spec = parse_template(text)
        out[spec.record_type] = spec
    return out


def _registry_key(project_root: Path | None, profiles: list[str] | None) -> tuple:
    """Cache key: the scan result depends only on (selected dirs, yaml files'
    mtime/size, the lock's mtime/size) — a profile edit invalidates the entry."""
    dirs = normalize_profiles(profiles) if profiles else default_profile_dirs(project_root)
    lock_stat: tuple = ()
    if project_root is not None:
        lock = project_root / ".proj.lock"
        if lock.is_file():
            st = lock.stat()
            lock_stat = (st.st_mtime_ns, st.st_size)
    key_files: list[tuple] = []
    for base in (bundled_dir(),
                 (project_root / "profiles" if project_root else None)):
        if base is None or not base.is_dir():
            continue
        for f in sorted(base.glob("*.yaml")):
            st = f.stat()
            key_files.append((str(f), st.st_mtime_ns, st.st_size))
        for p in dirs:
            d = base / p
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.yaml")):
                st = f.stat()
                key_files.append((str(f), st.st_mtime_ns, st.st_size))
    return (tuple(dirs), lock_stat, tuple(key_files))


def load_registry(project_root: Path | None = None,
                  profiles: list[str] | None = None) -> dict[str, RecordTypeSpec]:
    """RecordType registry: bundled profiles as the base, the project's own
    profiles/ overrides them per record_type. Only the *selected* profile
    subdirectories are scanned (the lock-pinned list, 'default' first, or the
    explicit `profiles` when given — init passes it before the lock exists) —
    unselected profile dirs are inert.

    In-process cache (see _REGISTRY_CACHE): the returned dict is a fresh shallow
    copy per call; the spec objects inside are shared and must be treated
    read-only (every caller is)."""
    key = _registry_key(project_root, profiles)
    cached = _REGISTRY_CACHE.get(key)
    if cached is None:
        dirs = normalize_profiles(profiles) if profiles else default_profile_dirs(project_root)
        cached = _scan(bundled_dir(), dirs)
        project = project_root / "profiles" if project_root else None
        if project is not None and project.is_dir():
            cached.update(_scan(project, dirs))
        _REGISTRY_CACHE.clear()   # bounded: keep only the most recent project
        _REGISTRY_CACHE[key] = cached
    return dict(cached)


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
    data = _yaml_load(changelog_path(root).read_text(encoding="utf-8"))
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
        data = _yaml_load(texts.read_text(encoding="utf-8"))
    else:
        data = _yaml_load((bundled_dir() / "texts.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("texts.yaml: expected a mapping at the top level")
    return data


def slugify(title: str) -> str:
    """A10: keep Unicode letters/digits (CJK etc.), lower ASCII, collapse
    every whitespace/punctuation run to a single "-", cap at 50 chars, no
    leading/trailing "-". A title with no alnum chars yields "" (callers
    fall back to the id alone, e.g. T-000001.md)."""
    parts: list[str] = []
    for ch in title.lower():
        if ch.isalnum():
            parts.append(ch)
        elif parts and parts[-1] != "-":
            parts.append("-")
    return "".join(parts).strip("-")[:50].rstrip("-")
