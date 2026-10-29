"""ADR-specific logic on top of the generic record engine."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from proj.config import ProjConfig
from proj.records import Record, RecordSet

ADR_TEMPLATE = """\
# {number}. {title}

Date: {date}

## Status

{status}

## Context

[Problem statement & constraints]

## Decision

[Chosen solution & why over alternatives]

## Consequences

[Pros, cons, tech debt, and follow-ups]
"""

SEEN_RE = re.compile(r"^[-*]\s*See also:\s*(.+)$", re.MULTILINE)


def record_set(cfg: ProjConfig) -> RecordSet:
    return RecordSet(cfg.adr_dir, prefix="ADR-")


def new_adr(cfg: ProjConfig, title: str, supersede: str | None = None) -> Path:
    rs = record_set(cfg)
    status = "Proposed"
    if supersede:
        # mark the old ADR as superseded by the new one
        old = by_number(rs, supersede)
        if old is None:
            raise ValueError(f"ADR-{supersede} not found in {cfg.adr_dir}")
        new_num = rs.next_number()
        rs.set_status(old, f"Superseded by ADR-{new_num}")
        status = f"Supersedes ADR-{f'{int(supersede):04d}' if supersede.isdigit() else supersede}"
    number = rs.next_number()
    # heading convention: un-padded number ("# 1. Title"), filename stays NNNN-padded
    body = ADR_TEMPLATE.format(number=int(number), title=title,
                               date=date.today().isoformat(), status=status)
    return rs.create(title, body, number=number)


def by_number(rs: RecordSet, number: str) -> Record | None:
    for rec in rs.discover():
        if rec.number == number or (number.isdigit() and rec.number == f"{int(number):04d}"):
            return rec
    return None


def link_adr(cfg: ProjConfig, target: str, links: list[str]) -> Record:
    """Append cross-references to a target ADR's 'See also' list."""
    rs = record_set(cfg)
    rec = by_number(rs, target)
    if rec is None:
        raise ValueError(f"ADR-{target} not found")
    text = rec.path.read_text(encoding="utf-8")
    items = [f"ADR-{n}" for n in links]
    m = SEEN_RE.search(text)
    if m:
        existing = [i.strip() for i in m.group(1).split(",")]
        items = existing + [i for i in items if i not in existing]
        text = text[: m.start()] + "See also: " + ", ".join(items) + text[m.end():]
    else:
        text = text.rstrip() + "\n\nSee also: " + ", ".join(items) + "\n"
    rec.path.write_text(text, encoding="utf-8")
    return rec


def toc(rs: RecordSet) -> str:
    """Markdown table of contents for all ADRs."""
    lines = ["# ADRs", ""]
    for rec in rs.discover():
        lines.append(f"- {rec.display}")
    return "\n".join(lines) + "\n"


def graph(rs: RecordSet) -> str:
    """Mermaid graph: superseded ADR -> superseding ADR."""
    recs = rs.discover()
    lines = ["graph LR"]
    edges = []
    for rec in recs:
        label = f"A{rec.number}[\"{rec.number} {rec.title[:24]}\"]"
        lines.append(f"  {label}")
        if rec.superseded_by:
            edges.append(f"  A{rec.number} --> A{rec.superseded_by}")
    lines.extend(edges)
    return "\n".join(lines) + "\n"
