"""Generic numbered-markdown-record engine.

A "record" is a file named ``NNNN-short-title.md`` (or ``YYYY-MM-DD-title.md``)
in a directory, with a first heading that must match the filename number and a
``## Status`` section. Used for ADRs, tech-debt, experiments; incidents use
date-based naming.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

NUMBERED = re.compile(r"^(\d{4})-(.+)\.md$")
DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$")

# Status line we accept anywhere under "## Status": "Accepted", "Deprecated",
# "Superseded by ADR-NNNN", "Repaid (date)", "Closed (date)", "Proposed".
STATUS_RE = re.compile(
    r"^\s*[-*]?\s*(Accepted|Deprecated|Proposed|Supersedes ADR[- ]?\d+"
    r"|Superseded by ADR[- ]?(\d+)"
    r"|Repaid(?: on \d{4}-\d{2}-\d{2})?|Closed(?: on \d{4}-\d{2}-\d{2})?)\s*:?\s*$",
    re.IGNORECASE,
)


@dataclass
class Record:
    path: Path
    number: str  # "0007" or "2026-10-28"
    slug: str
    title: str
    status: str
    superseded_by: str | None = None

    @property
    def display(self) -> str:
        sup = f" (→ {self.superseded_by})" if self.superseded_by else ""
        return f"{self.number}  {self.status:<12} {self.title}{sup}"


class RecordSet:
    """A directory of numbered or dated markdown records."""

    def __init__(self, directory: Path, dated: bool = False, prefix: str = "") -> None:
        self.dir = directory
        self.dated = dated
        self.prefix = prefix  # e.g. "ADR-" used in supersede references

    # -- discovery -------------------------------------------------------
    def _match(self, name: str) -> tuple[str, str] | None:
        pat = DATED if self.dated else NUMBERED
        m = pat.match(name)
        if m:
            return m.group(1), m.group(2)
        return None

    def discover(self) -> list[Record]:
        if not self.dir.is_dir():
            return []
        out: list[Record] = []
        for p in sorted(self.dir.iterdir()):
            m = self._match(p.name)
            if m and p.suffix == ".md":
                out.append(self._parse(p, m[0], m[1]))
        return out

    def _parse(self, path: Path, number: str, slug: str) -> Record:
        text = path.read_text(encoding="utf-8")
        title = self._extract_title(text)
        status, sup = self._extract_status(text)
        return Record(path=path, number=number, slug=slug, title=title,
                      status=status, superseded_by=sup)

    @staticmethod
    def _extract_title(text: str) -> str:
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                # strip heading markers and a leading "N." numbering
                t = line.lstrip("#").strip()
                t = re.sub(r"^\d+\.\s*", "", t)
                return t
        return ""

    @staticmethod
    def _extract_status(text: str) -> tuple[str, str | None]:
        in_status = False
        sup: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^#{1,6}\s+status\s*$", stripped, re.IGNORECASE):
                in_status = True
                continue
            if in_status:
                if stripped.startswith("#"):  # next heading
                    break
                m = STATUS_RE.match(stripped)
                if m:
                    if sup is None:
                        sup = m.group(2)
                    return m.group(1).strip(), sup
        return "Unknown", None

    # -- numbering -------------------------------------------------------
    def next_number(self) -> str:
        if self.dated:
            return date.today().strftime("%Y-%m-%d")
        nums = [int(m[0]) for m in (self._match(p.name) for p in self.dir.glob("*.md")) if m]
        return f"{(max(nums) + 1) if nums else 1:04d}"

    # -- creation --------------------------------------------------------
    def create(self, title: str, body: str, number: str | None = None) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        number = number or self.next_number()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        path = self.dir / f"{number}-{slug}.md"
        path.write_text(body, encoding="utf-8")
        return path

    # -- mutation --------------------------------------------------------
    def set_status(self, record: Record, status: str) -> None:
        text = record.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        out: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            if not replaced and re.match(r"^#{1,6}\s+status\s*$", stripped, re.IGNORECASE):
                out.append(line)
                out.append("")
                out.append(status)
                replaced = True
                continue
            if replaced and STATUS_RE.match(stripped):
                replaced = False  # skip the old status line
                continue
            out.append(line)
        if not replaced:
            out += ["", "## Status", "", status]
        record.path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
