"""Task & plan lifecycle.

`task` = work in progress RIGHT NOW, tracked in TASKLIST.md (one block each):

    ## Title

    - [ ] atomic step
    - [ ] atomic step

`plan` = overall plans in docs/plans/ — drafts, shelved work, or finished
archives. `task done` freezes completed work into docs/plans/;
`plan shelve` parks unfinished work there; `plan resume` brings it back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

BLOCK_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
STEP_RE = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.*)$")

TASK_TEMPLATE = """\
## {title}

- [ ] break into atomic, verifiable sub-steps
"""

PLAN_TEMPLATE = """\
# {title}

> Plan created {date}. Resume into TASKLIST.md with `proj plan resume <title>`.

- [ ] break into atomic, verifiable sub-steps
"""


@dataclass
class Task:
    title: str
    steps: list[str]  # step text
    done: list[bool]  # parallel to steps

    @property
    def complete(self) -> bool:
        return bool(self.steps) and all(self.done)

    @property
    def progress(self) -> str:
        return f"{sum(self.done)}/{len(self.steps)}"


def _parse_blocks(text: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Return (header text before first block, [(title, block lines)])."""
    m = next((m for m in BLOCK_RE.finditer(text)), None)
    if m is None:
        return text, []
    header = text[: m.start()].rstrip()
    blocks: list[tuple[str, list[str]]] = []
    matches = list(BLOCK_RE.finditer(text))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((match.group(1), text[match.start(): end].rstrip().splitlines()))
    return header, blocks


def _task_from_block(title: str, lines: list[str]) -> Task:
    steps: list[str] = []
    done: list[bool] = []
    for line in lines:
        m = STEP_RE.match(line)
        if m:
            steps.append(m.group(2).strip())
            done.append(m.group(1).lower() == "x")
    return Task(title=title, steps=steps, done=done)


def read_tasks(cfg) -> tuple[str, list[tuple[str, list[str], Task]]]:
    path = cfg.root / "TASKLIST.md"
    if not path.is_file():
        return "", []
    header, blocks = _parse_blocks(path.read_text(encoding="utf-8"))
    return header, [(t, ln, _task_from_block(t, ln)) for t, ln in blocks]


def write_tasks(cfg, header: str, blocks: list[tuple[str, list[str]]]) -> None:
    path = cfg.root / "TASKLIST.md"
    parts = [header] if header else []
    if blocks:
        parts.append("")
    for _title, lines in blocks:
        parts.append("\n".join(lines))
        parts.append("")
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


def add_task(cfg, title: str) -> None:
    header, blocks = read_tasks(cfg)
    if any(t.lower() == title.lower() for t, _, _ in blocks):
        raise ValueError(f"task '{title}' already exists in TASKLIST.md")
    if not header:
        header = "# Tasklist"
    blocks.append((title, TASK_TEMPLATE.format(title=title).strip().splitlines()))
    write_tasks(cfg, header, blocks)


def done_task(cfg, title: str) -> Path:
    """Freeze a fully-completed task into docs/plans/YYYY-MM-title.md."""
    header, blocks = read_tasks(cfg)
    for i, (t, lines, task) in enumerate(blocks):
        if t.lower() != title.lower():
            continue
        if not task.complete:
            raise ValueError(f"task '{title}' is not complete ({task.progress})")
        blocks.pop(i)
        write_tasks(cfg, header, blocks)
        archive = _plan_path(cfg, title)
        # frozen record: heading + final checklist state (drop the block heading)
        archived = ["# " + title, "",
                    "> Archived " + date.today().isoformat(), *lines[1:]]
        archive.write_text("\n".join(archived).rstrip() + "\n", encoding="utf-8")
        return archive
    raise ValueError(f"task '{title}' not found")


def _plan_path(cfg, title: str) -> Path:
    cfg.plans_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    p = cfg.plans_dir / f"{date.today().strftime('%Y-%m')}-{slug}.md"
    n = 1
    while p.exists():
        n += 1
        p = cfg.plans_dir / f"{date.today().strftime('%Y-%m')}-{slug}-{n}.md"
    return p


def shelve_task(cfg, title: str) -> Path:
    """Park an in-progress (incomplete) task into docs/plans/ as a shelved plan."""
    header, blocks = read_tasks(cfg)
    for i, (t, lines, task) in enumerate(blocks):
        if t.lower() != title.lower():
            continue
        blocks.pop(i)
        write_tasks(cfg, header, blocks)
        archive = _plan_path(cfg, title)
        body = ["# " + title, "",
                f"> Shelved {date.today().isoformat()} ({task.progress} done)",
                *lines[1:]]
        archive.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
        return archive
    raise ValueError(f"task '{title}' not found")


def resume_plan(cfg, title: str) -> None:
    """Move a plan from docs/plans/ back into TASKLIST.md as an active task."""
    plans = _find_plan(cfg, title)
    if not plans:
        raise ValueError(f"plan '{title}' not found in {cfg.plans_dir}")
    if len(plans) > 1:
        names = ", ".join(p.name for p in plans)
        raise ValueError(f"ambiguous plan '{title}': {names}")
    p = plans[0]
    lines = p.read_text(encoding="utf-8").splitlines()
    steps = [s for s in lines if STEP_RE.match(s)]
    if not steps:
        raise ValueError(f"plan {p.name} has no checklist steps to resume")
    header, blocks = read_tasks(cfg)
    if any(t.lower() == title.lower() for t, _, _ in blocks):
        raise ValueError(f"task '{title}' is already active in TASKLIST.md")
    if not header:
        header = "# Tasklist"
    block = ["## " + title, "", *steps]
    blocks.append((title, block))
    write_tasks(cfg, header, blocks)
    p.unlink()


def new_plan(cfg, title: str) -> Path:
    """Create a draft overall plan in docs/plans/."""
    p = _plan_path(cfg, title)
    p.write_text(PLAN_TEMPLATE.format(title=title, date=date.today().isoformat()),
                 encoding="utf-8")
    return p


def _find_plan(cfg, title: str) -> list[Path]:
    if not cfg.plans_dir.is_dir():
        return []
    out = []
    for p in cfg.plans_dir.glob("*.md"):
        first = p.read_text(encoding="utf-8").splitlines()
        head = next((ln.lstrip("#").strip() for ln in first if ln.startswith("# ")), p.stem)
        if head.lower() == title.lower():
            out.append(p)
    return out


def list_plans(cfg) -> list[tuple[Path, str, str]]:
    """Return (path, title, note) for every plan in docs/plans/."""
    out: list[tuple[Path, str, str]] = []
    if not cfg.plans_dir.is_dir():
        return out
    for p in sorted(cfg.plans_dir.glob("*.md"), reverse=True):
        lines = p.read_text(encoding="utf-8").splitlines()
        title = next((ln.lstrip("#").strip() for ln in lines if ln.startswith("# ")), p.stem)
        note = next((ln.lstrip("> ").strip() for ln in lines if ln.startswith("> ")), "")
        out.append((p, title, note))
    return out
