"""YAML frontmatter split/join for markdown files."""
from __future__ import annotations

import yaml

FM = "---"


def split(text: str) -> tuple[dict, str]:
    """Split markdown into (frontmatter_dict, body). No frontmatter → ({}, text)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FM:
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == FM:
            meta = yaml.safe_load("".join(lines[1:i])) or {}
            return meta, "".join(lines[i + 1:])
    return {}, text


def join(meta: dict, body: str) -> str:
    """Join frontmatter dict + body → markdown text."""
    if not meta:
        return body
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False,
                        allow_unicode=True, width=1000)
    return f"{FM}\n{fm}{FM}\n{body}"


def sections(body: str) -> dict[str, str]:
    """Split body into {heading_lower: content} at ## level."""
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur = line[3:].strip().lower()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out
