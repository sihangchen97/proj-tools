"""Project event log (git collab, design §3 + impl spec C2.2 / C3.1).

`.proj/events.jsonl` is the committed source of truth: one JSON event per
line (sorted keys, no extra whitespace, every line ends with `\n`), append
only. The DB is a replay of these lines (deterministic, `replay.py`).

Common event fields (C2.2): `at` (HLC ms), `seq` (per-process), `actor`
(`config.reporter()`), `rec` (record uuid, `""` for project-level events),
`op`. Op payloads (C2.2 table): create / set / section.set / item.add /
item.set / item.move / group.add / group.set / group.move / rel.add /
rel.del / wip.add / wip.del / log / changelog.add / changelog.release /
delete.

This module only knows the wire format + ordering + append/read — the event
emitter (C2) and the replayer (C3) live elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

#: every op a well-formed event may carry (E7; the replayer knows the same
#: set — `snapshot` is added by replay.py's compact support, C6)
OPS = ("create", "set", "section.set", "item.add", "item.set", "item.move",
       "group.add", "group.set", "group.move", "rel.add", "rel.del",
       "wip.add", "wip.del", "log", "changelog.add", "changelog.release",
       "delete")
#: the fields every event carries, whatever its op (C2.2)
REQUIRED_FIELDS = ("at", "actor", "op", "rec", "seq")

#: the sort key's last component guarantees a total order (design P2)
def sort_key(ev: dict) -> tuple:
    """(at, actor, seq, rec, sha1 of the serialized line) — the only total
    order every clone computes the same way, independent of line order."""
    return (ev["at"], ev["actor"], ev["seq"], ev["rec"],
            hashlib.sha1(to_line(ev).encode("utf-8")).hexdigest())


def make(op: str, *, actor: str, at: int, seq: int, rec: str = "",
         **payload: object) -> dict:
    """One event: common fields + the op's payload (payload field names per
    the C2.2 table; validated by the replayer, not here)."""
    return {"at": at, "actor": actor, "op": op, "rec": rec, "seq": seq,
            **payload}


def to_line(ev: dict) -> str:
    """One line, keys sorted, no extra whitespace, ending in `\\n`."""
    return json.dumps(ev, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"


def from_line(line: str) -> dict:
    """Parse one event line (trailing newline ok). json.JSONDecodeError
    propagates with the parser's position; the caller adds the line number."""
    ev = json.loads(line)
    if not isinstance(ev, dict):
        raise ValueError(f"an event must be a JSON object: {line!r}")
    return ev


class HlcClock:
    """Hybrid logical clock (design §4): `at = max(now, last seen at) + 1ms`,
    so an actor's later events always sort after the events they have seen
    (even on a skewed clock); `last` is kept in the local meta (C2.3)."""

    def __init__(self, last_at: int = 0) -> None:
        self._last = last_at

    def tick(self) -> int:
        now = int(time.time() * 1000)
        self._last = max(now, self._last) + 1
        return self._last

    def advance_to(self, at: int) -> None:
        """Seen an event this late (e.g. replayed it) — do not emit before it."""
        if at > self._last:
            self._last = at


def append(path: Path | str, events: list[dict]) -> int:
    """Append every event with ONE os.write (O_APPEND): atomic enough that a
    crash never interleaves lines, and the file always ends with `\\n`
    (union merge depends on it). Returns the bytes written."""
    if not events:
        return 0
    buf = "".join(to_line(ev) for ev in events).encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        return os.write(fd, buf)
    finally:
        os.close(fd)


def read(path: Path | str) -> list[dict]:
    """All events, deduplicated (union merge can keep a line twice) and
    ordered by the total sort key. A line that is not valid JSON raises
    ValueError naming the line number; a non-empty file not ending in
    `\\n` also raises (its last line would have fused with a remote append).
    A missing file yields [] (no events yet)."""
    p = Path(path)
    if not p.is_file():
        return []
    raw = p.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError(f"{p}: does not end with a newline — its last event "
                         "line would fuse with an append after a union merge")
    parsed: dict[str, dict] = {}  # exact line -> parsed event (dedup)
    for i, line in enumerate(raw.split(b"\n"), start=1):
        if not line:
            continue
        text = line.decode("utf-8")
        try:
            ev = from_line(text)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            raise ValueError(f"{p}:{i}: bad event line ({e}): {text!r}") from e
        parsed.setdefault(text, ev)
    events = sorted(parsed.values(), key=sort_key)
    return events


def validate(path: Path | str) -> list[str]:
    """E7 (C6 `proj check` item, wiring done by alex in C6): every problem
    with the event log as a list of messages; [] = healthy.

    Per line: parseable (the message names the line number), no git conflict
    markers, one JSON object, all common fields present (`at`/`actor`/`op`/
    `rec`/`seq`), `op` in `OPS`. Per file: ends with `\\n` (a missing file is
    healthy — no events yet; an empty file is fine)."""
    p = Path(path)
    if not p.is_file():
        return []
    problems: list[str] = []
    raw = p.read_bytes()
    if raw and not raw.endswith(b"\n"):
        problems.append(f"{p}: does not end with a newline")
    for i, line in enumerate(raw.split(b"\n"), start=1):
        if not line:
            continue
        text = line.decode("utf-8", errors="replace")
        if "<<<<<<<" in text or ">>>>>>>" in text or text.startswith("|||||||"):
            problems.append(f"{p}:{i}: git conflict marker in event line")
            continue
        try:
            ev = from_line(text)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            problems.append(f"{p}:{i}: bad event line ({e})")
            continue
        missing = [f for f in REQUIRED_FIELDS if f not in ev]
        if missing:
            problems.append(
                f"{p}:{i}: missing field(s) {', '.join(missing)}")
            continue
        if ev["op"] not in OPS:
            problems.append(f"{p}:{i}: unknown op {ev['op']!r}")
    return problems
