"""Derived index at .proj/index.json — rebuilt from record files, never source."""
from __future__ import annotations

import json
import re
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path

from proj.frontmatter import sections, split
from proj.templates import KindSpec, load_registry, render, slugify

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
INDEX_VERSION = 1


@dataclass
class FMRecord:
    """A frontmatter-based record (v2 engine)."""
    kind: str
    path: Path
    meta: dict = field(default_factory=dict)
    body: str = ""

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def uuid(self) -> str:
        return str(self.meta.get("uuid", ""))

    @property
    def title(self) -> str:
        return str(self.meta.get("title", ""))

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))

    @property
    def created(self) -> str:
        return str(self.meta.get("created", ""))

    @property
    def history(self) -> list[dict]:
        return list(self.meta.get("history") or [])

    @property
    def display(self) -> str:
        return f"{self.id}  {self.status:<12} {self.title}"

    def add_event(self, event: str, **kw) -> None:
        h = self.meta.setdefault("history", [])
        entry = {"date": kw.pop("date"), "event": event}
        entry.update(kw)
        h.append(entry)

    def status_set(self, status: str, when: str | None = None) -> None:
        from datetime import date
        old = self.status
        self.meta["status"] = status
        self.add_event("status", date=when or date.today().isoformat(),
                       **({"from": old, "to": status} if old and old != status
                          else {"to": status}))

    def write(self) -> None:
        from proj.frontmatter import join
        self.path.write_text(join(self.meta, self.body), encoding="utf-8")

    def to_index(self, rel: Path) -> dict:
        return {
            "id": self.id, "uuid": self.uuid, "title": self.title,
            "status": self.status, "created": self.created,
            "path": str(rel), "sections": sections(self.body),
        }


def load(cfg) -> dict:
    p = cfg.proj_dir / "index.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"version": INDEX_VERSION, "records": {}}


def save(cfg, index: dict) -> None:
    cfg.proj_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.proj_dir / "index.json"
    p.write_text(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                 encoding="utf-8")


def read_record(cfg, spec: KindSpec, path: Path) -> FMRecord | None:
    if not path.is_file():
        return None
    meta, body = split(path.read_text(encoding="utf-8"))
    if not meta:
        return None  # not frontmatter-managed (e.g. legacy v1 ADR)
    return FMRecord(kind=spec.kind, path=path, meta=meta, body=body)


def next_id(cfg, spec: KindSpec) -> str:
    if spec.numbered <= 0:
        from datetime import date
        return date.today().strftime("%Y-%m-%d")
    nums = [int(p.name[:spec.numbered]) for p in (cfg.root / spec.dir).glob("*.md")
            if p.name[:spec.numbered].isdigit()]
    return f"{(max(nums) + 1) if nums else 1:0{spec.numbered}d}"


def new_record(cfg, spec: KindSpec, title: str, supersedes: str = "") -> FMRecord:
    """Render template → write file → return in-memory record (not yet indexed)."""
    directory = cfg.root / spec.dir
    directory.mkdir(parents=True, exist_ok=True)
    id_ = next_id(cfg, spec)
    from datetime import date
    body = render(spec, uuid=str(_uuid.uuid4()), id_=id_, title=title,
                  date=date.today().isoformat(), supersedes=supersedes)
    path = directory / f"{id_}-{slugify(title)}.md"
    path.write_text(body, encoding="utf-8")
    meta, b = split(body)
    return FMRecord(kind=spec.kind, path=path, meta=meta, body=b)


def upsert(cfg, rec: FMRecord) -> dict:
    """Insert/update one record in the index and save it. Returns the index."""
    index = load(cfg)
    index["records"].setdefault(rec.kind, {})[rec.uuid] = rec.to_index(
        rec.path.relative_to(cfg.root))
    save(cfg, index)
    return index


def rebuild(cfg) -> tuple[dict, list[str]]:
    """Full rescan of all kinds. Returns (index, problems)."""
    registry = load_registry(cfg.root)
    index: dict = {"version": INDEX_VERSION, "records": {}}
    problems: list[str] = []
    for kind, spec in sorted(registry.items()):
        records: dict[str, dict] = {}
        d = cfg.root / spec.dir
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                rec = read_record(cfg, spec, p)
                if rec is None:
                    problems.append(f"{p.name}: no frontmatter — not index-managed")
                    continue
                if not UUID_RE.match(rec.uuid):
                    problems.append(f"{p.name}: bad uuid {rec.uuid!r}")
                    continue
                records[rec.uuid] = rec.to_index(p.relative_to(cfg.root))
        index["records"][kind] = records
    save(cfg, index)
    return index, problems


def all_records(cfg) -> list[FMRecord]:
    """All frontmatter-managed records across all kinds (reads from disk)."""
    registry = load_registry(cfg.root)
    out: list[FMRecord] = []
    for spec in registry.values():
        d = cfg.root / spec.dir
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            rec = read_record(cfg, spec, p)
            if rec is not None:
                out.append(rec)
    return out


def by_id(cfg, kind: str, id_: str) -> FMRecord | None:
    registry = load_registry(cfg.root)
    if kind not in registry:
        return None
    spec = registry[kind]
    d = cfg.root / spec.dir
    if not d.is_dir():
        return None
    for p in sorted(d.glob("*.md")):
        if p.name.startswith(f"{id_}-") or (id_.isdigit() and
                                            p.name.startswith(f"{int(id_):0{spec.numbered}d}-")):
            rec = read_record(cfg, spec, p)
            if rec is not None:
                return rec
    return None


def validate_record(cfg, rec: FMRecord, index: dict) -> list[str]:
    """Submit contract for one record. Returns violation messages."""
    problems: list[str] = []
    spec = load_registry(cfg.root).get(rec.kind)
    if spec is None:
        return [f"{rec.path.name}: unknown kind {rec.kind!r}"]
    # uuid
    if not UUID_RE.match(rec.uuid):
        problems.append(f"{rec.path.name}: bad uuid {rec.uuid!r}")
    else:
        for kind_recs in index["records"].values():
            for u, entry in kind_recs.items():
                if u == rec.uuid and entry["path"] != str(rec.path.relative_to(cfg.root)):
                    problems.append(f"{rec.path.name}: uuid {rec.uuid} already used "
                                    f"by {entry['path']}")
    # id == filename
    if rec.id and not rec.path.name.startswith(f"{rec.id}-"):
        problems.append(f"{rec.path.name}: id {rec.id!r} does not match filename")
    # status allowed — entries are full-string regexes (e.g. "Superseded by DEC-\\d{4}")
    if rec.status and spec.status_allowed and \
            not any(re.fullmatch(allowed, rec.status) for allowed in spec.status_allowed):
        problems.append(f"{rec.path.name}: status {rec.status!r} not in "
                        f"{spec.status_allowed}")
    # required fields
    for f in ("id", "uuid", "title", "status", "created"):
        if not rec.meta.get(f):
            problems.append(f"{rec.path.name}: missing frontmatter field {f!r}")
    # history shape
    for i, h in enumerate(rec.history):
        if not isinstance(h, dict) or "date" not in h or "event" not in h:
            problems.append(f"{rec.path.name}: history[{i}] needs date+event")
    return problems
