"""WIP.md (plans §5) — the project-root "what is being worked on" list.

Membership is REGISTERED (the ``wip_entries`` table), not derived: any record
type may sit in it, added manually (``proj wip add``) or automatically when a
record switches into a status listed in its type's ``wip.enter``. The file is
tool-owned except the item lines (and their ``###`` group headings) — those are
edit in place and ``proj sync`` writes them back into the record (``items`` is
the only writer of the checklist rows).

The per-entry item section is rendered by ``items.render_section`` — byte-for-byte
the same shape as the record md's checklist — so sync can hand it back to
``items.sync_section`` (see ``wip_sync``).
"""
from __future__ import annotations

import os

from proj.core.config import ProjConfig
from proj.core.templates import RecordTypeSpec
from proj.engine.views import View

WIP_FILE = "WIP.md"
ENTRY_MARK = "<!-- proj:wip-entry {id} -->"
WIP_HEADER = ("<!-- proj:wip — TOOL-MANAGED except item lines and group "
              "headings; edit items here, `proj sync` writes them back -->")
FOOTER = ("_Rendered by `proj update` — never hand-edit the headings, metadata "
          "and summary; the item lines are yours to tick and `proj sync` writes "
          "them back. Add/remove with `proj wip add` / `proj wip return`._")


def closed_states(spec: RecordTypeSpec) -> set[str]:
    """The closed item states (P4, global: done/failed/dropped)."""
    from proj.engine.items import CLOSED_STATES  # lazy: wip<->items cycle
    return set(CLOSED_STATES)


def _record_by_id(db, record_id: str) -> dict | None:
    """A record row straight from the DB (type included) — the registry is keyed
    by record_type, and WIP entries are addressed by id alone."""
    row = db.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def _progress(row: dict, spec: RecordTypeSpec, db) -> str:
    """The metadata line's `n/m`: the checklist's closed/total items; for a task
    WITHOUT checklist items, its terminal/total sub-tasks."""
    from proj.engine import items as items_mod  # lazy: wip↔items cycle
    if spec.wip_items is not None:
        live = items_mod.load(db, row["id"], spec).live()
        items = [i for i in live if not i.is_group]
        if items:
            closed = [i for i in items if i.state in closed_states(spec)]
            return f"{len(closed)}/{len(items)}"
    if spec.record_type == "task":
        kids = [c["target"] for c in db.children_of(row["id"])]
        if kids:
            term = [k for k in kids
                    if (r := _record_by_id(db, k)) is not None
                    and r["status"] in spec.status_terminal]
            return f"{len(term)}/{len(kids)}"
    return ""


def entry_lines(row: dict, spec: RecordTypeSpec, cfg: ProjConfig,
                db) -> list[str]:
    """One WIP entry (plans §5.4): `## ID. title` + entry marker + metadata
    line + summary + the checklist (its item section, groups as `###` lines)."""
    from proj.engine import items as items_mod  # lazy
    from proj.engine.store import slugify  # lazy: wip↔store cycle

    stem = f"{row['id']}-{slugify(row['title'])}"
    rel = f"{spec.dir}/{stem}.md".replace(os.sep, "/")  # path from the project root
    meta = f"> {spec.emojify(row['status'])} {row['status']} · {spec.record_type} · [{stem}]({rel})"
    prog = _progress(row, spec, db)
    if prog:
        meta += f" · {prog}"
    lines = [f"## {row['id']}. {row['title']}", ENTRY_MARK.format(id=row["id"]), meta, ""]
    if row.get("summary"):
        lines += [f"_{row['summary'].strip()}_", ""]
    if spec.wip_items is not None:
        state = items_mod.load(db, row["id"], spec)
        state.intro = items_mod._intro_of(spec, row)
        sec = items_mod.render_section(state, spec)
        if sec.strip():
            lines += sec.splitlines()
    return lines


def render_wip_md(cfg: ProjConfig, db, registry: dict[str, RecordTypeSpec]) -> View:
    """The global WIP.md (plans §5.4): one section per registered entry, in list
    order (pos). Always created; shows `_nothing in progress_` when empty. A
    marker for a record that is no longer in the DB is dropped (it re-renders
    on the next write) — removal belongs to `proj wip return`."""
    lines = [WIP_HEADER, "# WIP", ""]
    body: list[str] = []
    for entry in db.wip_all():
        row = _record_by_id(db, entry["record_id"])
        if row is None:
            continue
        body += entry_lines(row, registry[row["type"]], cfg, db)
        body.append("")
    lines += body or ["_nothing in progress_"]
    lines += ["", "---", ""]
    lines += [FOOTER]
    return View(cfg.root / WIP_FILE, "\n".join(lines).rstrip("\n") + "\n")


# ---- reading the editable item section back (§5.5) ------------------------------

def _entry_slice(wip_text: str, record_id: str) -> str:
    """The body of one entry (from its `## ID.` heading to the next `## ` or EOF) —
    '' when the entry is absent (a hand-removed entry is NOT a return: it
    re-renders, and only `proj wip return` removes it, §5.5)."""
    start = wip_text.find(f"## {record_id}. ")
    if start < 0:
        return ""
    nxt = wip_text.find("\n## ", start + 1)
    return wip_text[start: nxt if nxt > 0 else len(wip_text)]


def entry_items_text(wip_text: str, record_id: str) -> str:
    """Just the editable item lines of an entry (its checklist as rendered in the
    record md): the contiguous run of `- [` / `### ` / item-trailer lines from the
    first item line. Stops at a blank line followed by non-item text (the entry
    summary or the file footer are tool-owned and must not be handed back). The
    heading, marker and metadata line are tool-owned and ignored."""
    body = _entry_slice(wip_text, record_id)
    if not body:
        return ""
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("- [") or line.startswith("### ")), None)
    if start is None:
        return ""
    out: list[str] = []
    for line in lines[start:]:
        s = line.strip()
        if line.startswith("- [") or line.startswith("### "):
            out.append(line)
            continue
        if out and (s == "" or s.startswith(("- ", "* ", "`"))):
            out.append(line)  # a blank/continuation line inside the checklist
            continue
        break  # a non-item paragraph: the summary or the footer — stop
    return "\n".join(out).rstrip()


def _changed(db, spec: RecordTypeSpec, record_id: str, md_section: str) -> bool:
    """Does an item section differ from the DB's checklist? (compare the rendered
    section to the stored rows; the intro is not part of WIP.md)."""
    from proj.engine import items as items_mod  # lazy
    state = items_mod.load(db, record_id, spec)
    return items_mod.normalize(md_section) != items_mod.normalize(
        items_mod.render_section(state, spec))


def wip_sync_plan(db, registry: dict[str, RecordTypeSpec], cfg: ProjConfig) -> tuple[dict, list]:
    """The §5.5 pre-write scan (run in store.sync BEFORE any DB write): for each
    WIP entry whose item section drifted in WIP.md, decide whether its record md
    ALSO drifted (a CONFLICT — neither side may be written, the record is kept as
    is) or only WIP.md drifted (write WIP.md back, and skip the record md's items
    section in the ingest). Returns (wip_only: {record_id: section}, conflicts:
    [record_id]). Read-only."""
    from proj.engine import items as items_mod  # lazy
    from proj.engine import store as rec  # lazy: wip↔store cycle
    wip_path = cfg.root / WIP_FILE
    wip_text = wip_path.read_text(encoding="utf-8") if wip_path.exists() else ""
    wip_only: dict[str, str] = {}
    conflicts: list[str] = []
    for entry in db.wip_all():
        rid = entry["record_id"]
        row = _record_by_id(db, rid)
        if row is None:
            continue
        spec = registry[row["type"]]
        if spec.wip_items is None:
            continue
        section = entry_items_text(wip_text, rid)
        if not (section and _changed(db, spec, rid, section)):
            continue  # WIP.md matches the DB: nothing to write back
        # the record md side: does the record file's items section differ from the DB?
        rec_drift = False
        p = rec.file_path(cfg, spec, row)
        if p.exists():
            _mk, _u, raw = rec.parse_record(p.read_text(encoding="utf-8"))
            rec_drift = items_mod.drift(db, spec, row,
                                        raw.get(spec.wip_items.section.lower(), ""))
        if rec_drift:
            conflicts.append(rid)  # both sides edited — keep the DB, flag it
        else:
            wip_only[rid] = section  # only WIP.md edited — write it back
    return wip_only, conflicts


def auto_status_wip(db, spec: RecordTypeSpec, record_id: str, old: str, to: str) -> None:
    """The automatic WIP side of a status SWITCH (called by store.status_set inside
    its transaction, W3/W6): entering a `wip.enter` status adds the record
    (manual-return-then-same-status never re-adds — only a real switch triggers);
    entering a TERMINAL status returns it, writing WIP.md's edited item section
    back first. Both write a changelog line."""
    from proj.engine import items as items_mod  # lazy
    if to in spec.wip_enter and not db.wip_has(record_id):
        db.wip_add(record_id, "auto")
        db.add_log(record_id, f"added to WIP (auto: {to})", category="Changed")
    if (to in spec.status_terminal and old not in spec.status_terminal
            and db.wip_has(record_id)):
        wip_text = (db.path.parent / WIP_FILE).read_text(encoding="utf-8") \
            if (db.path.parent / WIP_FILE).exists() else ""
        section = entry_items_text(wip_text, record_id)
        if section and _changed(db, spec, record_id, section):
            row = dict(db.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
            problems, _ = items_mod.sync_section(db, spec, row, section, source="WIP.md")
            if problems:
                raise ValueError(f"{record_id}: WIP.md write-back on auto-return failed:\n"
                                 + "\n".join(problems))
        db.wip_remove(record_id)
        db.add_log(record_id, f"returned from WIP (auto: {to})", category="Changed")


def wip_add(cfg, record_ids: list[str], source: str = "manual") -> tuple[list[str], list[str]]:
    """Register records in WIP.md (§5.6): a record already in it is skipped (not
    an error); the WIP.md + changelog (`added to WIP`) are re-rendered. One
    transaction."""
    from proj.engine import store as rec  # lazy: wip↔store cycle
    added, notes = [], []
    with rec.open_db(cfg) as db:
        db.begin()
        for rid in record_ids:
            row = _record_by_id(db, rid)
            if row is None:
                raise ValueError(f"{rid} not found")
            if db.wip_has(rid):
                notes.append(f"{rid} is already in WIP (skipped)")
                continue
            db.wip_add(rid, source)
            db.add_log(rid, "added to WIP", category="Changed")
            added.append(rid)
        rec.render_all(cfg, db)
    return added, notes


def wip_return(cfg, record_ids: list[str]) -> tuple[list[str], list[str]]:
    """Remove records from WIP.md (§5.6): first write WIP.md's (possibly edited,
    unsynced) item section back into the record, then un-register; a record not
    in WIP is an error. One transaction; the changelog says `returned from WIP`."""
    from proj.engine import store as rec  # lazy: wip↔store cycle
    wip_path = cfg.root / WIP_FILE
    wip_text = wip_path.read_text(encoding="utf-8") if wip_path.exists() else ""
    returned, notes = [], []
    with rec.open_db(cfg) as db:
        db.begin()
        for rid in record_ids:
            if not db.wip_has(rid):
                raise ValueError(f"{rid} is not in WIP")
            reg = rec.load_registry(cfg.root)
            spec = reg[_record_by_id(db, rid)["type"]]
            section = entry_items_text(wip_text, rid)
            if section and _changed(db, spec, rid, section):
                row = dict(db.conn.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())
                problems, n = _sync_items(db, spec, row, section, source="WIP.md")
                if problems:
                    raise ValueError(f"{rid}: WIP.md write-back failed:\n" + "\n".join(problems))
                notes += [f"WIP.md: {x}" for x in n]
            db.wip_remove(rid)
            db.add_log(rid, "returned from WIP", category="Changed")
            returned.append(rid)
        rec.render_all(cfg, db)
    return returned, notes


def wip_ls(cfg, owner: str | None = None) -> list[dict]:
    """The WIP list (id, status, title, closed/total) for `proj wip ls`.
    With `owner` (D8 / §20): only the entries whose WIP checklist has an OPEN
    item owned by that person, each carrying those items (`no`/`state`/`text`/
    `due`) — the per-WIP version of `item ls --owner --open`."""
    from proj.engine import items as items_mod  # lazy: wip↔items cycle
    from proj.engine import store as rec  # lazy
    with rec.open_db(cfg) as db:
        reg = rec.load_registry(cfg.root)
        out = []
        for entry in db.wip_all():
            row = _record_by_id(db, entry["record_id"])
            if row is None:
                continue
            spec = reg[row["type"]]
            d = {"id": row["id"], "status": row["status"], "type": row["type"],
                 "title": row["title"], "progress": _progress(row, spec, db),
                 "source": entry["source"], "owner_items": []}
            if owner is not None and spec.wip_items is not None:
                mine = [i for i in items_mod.load(db, row["id"], spec).live()
                        if not i.is_group and i.owner == owner
                        and i.state not in closed_states(spec)]
                d["owner_items"] = [{"no": i.no, "state": i.state, "text": i.text,
                                     "due": i.due} for i in mine]
                if not mine:  # only list entries that actually carry an open item
                    continue
            out.append(d)
        return out


def format_wip_ls(rows: list[dict], owner: str | None = None) -> str:
    """The WIP list as text (shared by the CLI and the MCP). With `owner`, the
    matching open items are listed under their entry (`  #no state text [due]`)."""
    if not rows:
        return "(no records in WIP)"
    lines = []
    w = max(len(r["id"]) for r in rows)
    s = max(len(r["status"]) for r in rows)
    for r in rows:
        lines.append(f"{r['id']:<{w}}  {r['status']:<{s}}  {r['title']}"
                     + (f"  {r['progress']}" if r["progress"] else ""))
        for it in r.get("owner_items", []):
            st = "x" if it["state"] in ("done", "failed", "dropped") else " "
            lines.append(f"    [{st}] #{it['no']} {it['text']}"
                         + (f"  (due {it['due']})" if it["due"] else ""))
    return "\n".join(lines)


def _sync_items(db, spec: RecordTypeSpec, row: dict, md_section: str,
                source: str) -> tuple[list[str], list[str]]:
    """Write one checklist section into the DB (items is the only writer); returns
    (problems, notes). The caller is inside the transaction."""
    from proj.engine import items as items_mod  # lazy
    problems, notes = items_mod.sync_section(db, spec, row, md_section, source=source)
    return problems, notes
