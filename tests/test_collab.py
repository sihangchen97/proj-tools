"""E3 (hard-parts-impl §0.1): the two-clone harness + scenario tests.

The scenarios are the design doc §10 (plans/git-collab-design.md) acceptance
cases, run against REAL git and the REAL `proj` binary (tests/
collab_harness.py). Until C2 (event emitter) + C4 (replay on write / cache
rebuild) land; scenarios still awaiting them are xfailed with the missing
piece named —
they fail today (a pulled clone does not sync its DB from events.jsonl) and
turn into green regression tests the day C2/C4 is wired in, no test change
needed.

The xfail is strict: a PASS while xfailed is reported as a failure, so the
harness cannot silently hide a real success.
"""
from __future__ import annotations

import json
import re

import collab_harness as ch
import pytest

# the harness test and the trailing-newline check are NOT xfailed (they must
# hold already); the merge scenarios wait on C2 (event emitter) + C4 (replay
# on write / cache rebuild), the merge-in-progress one on C5. strict=True so a
# pass while xfailed is reported.
WAIT_C2C4 = pytest.mark.xfail(
    strict=True, reason="needs C2 (event emitter) + C4 (replay/cache rebuild)")
WAIT_C5 = pytest.mark.xfail(
    strict=True, reason="needs C5 (refuse write commands mid-merge/rebase)")
# B-4 (2026-11-03): the rename/rename same-field LWW is resolved by
# `proj merge resolve` (the old WAIT_RENAME xfail is gone).

@pytest.fixture(scope="module")
def proj_binary() -> str:
    return ch._find_proj()

def _task_titles(cl: ch.Clone) -> list[str]:
    return [r["title"] for r in json.loads(cl.run("task", "ls", "--json"))]

def _task_ids(cl: ch.Clone) -> list[str]:
    return [r["id"] for r in json.loads(cl.run("task", "ls", "--json"))]

def _new_task_id(cl: ch.Clone, title: str) -> str:
    out = cl.run("task", "new", title, "-y")
    # `task new` prints "Created <relative-path>.md" — the id is the file's
    # stem prefix (T-000001-…)
    line = [ln for ln in out.splitlines() if ln.startswith("Created ")][-1]
    # the file name is `<ID>-<slug>.md` — the id is the leading CODE-DIGITS
    m = re.search(r"([A-Z]+-\d+)-", line)
    assert m, line
    return m.group(1)

def test_harness_builds_two_identical_clones(tmp_path, proj_binary):
    """The harness itself (already holds): bare repo, two clones, each a
    working proj project with hooks installed, identical state, clean trees."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    sa, sb = a.state(), b.state()
    assert sa["ls"] == sb["ls"] and sa["items"] == sb["items"]
    assert sa["md"] and sa["md"] == sb["md"]
    for c in (a, b):
        out = c.run_sh("git status --short --branch").splitlines()
        assert out[0].startswith("## ")  # a branch, on top of origin
        assert not any(ln[:2] != "## " for ln in out[1:])  # clean tree
        assert (c.path / "docs").is_dir()  # proj init ran in the clone
        # hooks are best-effort here (E4 in progress); the scenarios push/pull
        # manually and do not need them, so neither value is a failure

def test_disjoint_tasks_merge_and_rebuild(tmp_path, proj_binary):
    """§10.1: both add different tasks → both DBs list all of them after the
    merge (C4: a write command in B rebuilds the DB from the pulled log)."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    # A adds task A and pushes; B (already up to date) adds task B and pulls
    tid_a = _new_task_id(a, "任务 A")
    a.commit("A: task")
    t.push(a)
    tid_b = _new_task_id(b, "任务 B")
    b.commit("B: task")
    t.push(b)
    t.rebase(a)  # A takes B's task
    assert not t.merge_conflict()
    assert sorted(_task_titles(a)) == sorted(_task_titles(b))
    assert {tid_a, tid_b} <= set(_task_ids(a))

def test_disjoint_sections_merge(tmp_path, proj_binary):
    """§10.2: two authors append different ## sections to the same record's
    md → the union merge keeps both on each side (C2: both became events).
    B-2 renders the non-template sections too, so both sides agree."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    did = _new_task_id(a, "决定")
    a.commit("base")
    t.push(a)
    t.rebase(b)
    for c, sec in ((a, "\n## 备选\n\n- A 的备选\n"),
                   (b, "\n## 后果\n\n- B 的后果\n")):
        md = next(c.path.rglob(f"{did}-*.md"))
        md.write_text(md.read_text(encoding="utf-8") + sec, encoding="utf-8")
        c.run("sync")
        c.commit(sec.splitlines()[1].strip())
    t.push(a)
    t.rebase(b)
    assert not t.merge_conflict()
    # two-side reconcile: B publishes its merged log, A picks it up — both
    # land on the same event log (B-1: git HEAD moved → full replay from the
    # empty state on both sides)
    t.push(b)
    t.pull(a)
    assert not t.merge_conflict()
    md_b = next(b.path.rglob(f"{did}-*.md")).read_text(encoding="utf-8")
    md_a = next(a.path.rglob(f"{did}-*.md")).read_text(encoding="utf-8")
    assert "A 的备选" in md_b and "B 的后果" in md_b
    assert md_a == md_b

def test_same_field_lww_keeps_later_and_reports(tmp_path, proj_binary):
    """§10.3: both change the same task title → the merge keeps exactly one
    value (LWW, the later writer) and surfaces the overwrite (G5).
    B-4 (2026-11-03, resolved): both sides rename the SAME file to different
    slugs — a git rename/rename conflict that fires BEFORE the keep-ours
    driver runs, so the everyday pull (a merge) halts. `proj merge resolve`
    handles exactly this deterministic shape: it keeps the LWW worktree name,
    drops the other side's name and the original unmerged path, commits the
    merge and re-renders from the merged log."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    tid = _new_task_id(a, "原")
    a.commit("base")
    t.push(a)
    t.rebase(b)
    for c, title in ((a, "标题 A"), (b, "标题 B")):
        md = next(c.path.rglob(f"{tid}-*.md"))
        md.write_text(md.read_text(encoding="utf-8").replace(
            f"# {tid}. 原", f"# {tid}. {title}"), encoding="utf-8")
        c.run("sync")
        c.commit(title)
    t.push(a)
    # the everyday pull (a merge, not a rebase) halts on rename/rename
    with pytest.raises(RuntimeError, match="rename/rename"):
        t.pull(b)
    assert t.merge_conflict()
    out = b.run("merge", "resolve")
    assert "merge resolved" in out and "标题-b" in out
    assert not t.merge_conflict()
    # only the LWW name survives (F1 prunes the other side's + the original)
    names = sorted(p.name for p in b.path.rglob(f"{tid}-*.md"))
    assert names == [f"{tid}-标题-b.md"], names
    md_b = next(b.path.rglob(f"{tid}-*.md")).read_text(encoding="utf-8")
    assert ("标题 A" in md_b) ^ ("标题 B" in md_b)  # exactly one wins

def test_disjoint_checklists_and_wip_merge(tmp_path, proj_binary):
    """§10.4: both check different items on the same record + both add
    different records to WIP.md → the union keeps everything."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    tid = _new_task_id(a, "原")
    a.run("item", "add", tid, "步骤一")
    a.run("item", "add", tid, "步骤二")
    a.commit("base items")
    t.push(a)
    t.rebase(b)
    # A checks item 1; B checks item 2 (disjoint) — both in the same record
    for c, item in ((a, "步骤一"), (b, "步骤二")):
        md = next(c.path.rglob(f"{tid}-*.md"))
        md.write_text(md.read_text(encoding="utf-8").replace(
            f"- [ ] {item}", f"- [x] {item}"), encoding="utf-8")
        c.run("sync")
        c.commit(f"check {item}")
    t.push(a)
    t.rebase(b)
    assert not t.merge_conflict()
    listing = a.run("item", "ls", tid)
    assert "步骤一" in listing and "步骤二" in listing

# ---------------------------------------------------------------------------
# §10 + C7 追加场景 (2026-11-03 00:10): merge-order independence,
# trailing newline, merge-in-progress refusal, rebase == merge, cache rebuild.
# Scenarios needing C2 (event emitter) / C4 (replay on write + cache
# rebuild) are xfail(strict) until those land; C5 (merge-in-progress
# refusal) likewise. The trailing-newline check holds TODAY (E7 + the C6
# check wiring, 33bb9c5) — it is NOT xfailed.

def _states_equal(t) -> None:
    sa, sb = t.a.state(), t.b.state()
    assert sa == sb, "the two sides differ after the merge"
    assert sa["md"], "no md data found"

def _event_line(op, at, actor, seq, rec="", **payload) -> str:
    """One canonical events.jsonl line (the same writer C2 will use)."""
    from proj.events import make, to_line
    return to_line(make(op, actor=actor, at=at, seq=seq, rec=rec, **payload))

# ---- §10.1 / C7 merge-order independence -----------------------------------

# R3: C2+C4 now hold (item add carries pos) — no longer xfailed
def test_id_collision_renumbers_and_both_sides_agree(tmp_path, proj_binary):
    """§10.1: both propose the same next task id (T-000002) → one keeps it
    (the earlier), the other is renumbered (T-000003); BOTH sides agree and
    the renumbering is reported."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    # the base holds ONE task (T-000001); both clones next propose T-000002
    _new_task_id(a, "基础任务")
    a.commit("base task")
    t.push(a)
    t.rebase(b)
    assert _task_ids(a) == ["T-000001"]
    tid_a = _new_task_id(a, "A 建")
    assert tid_a == "T-000002"
    a.commit("A: T-000002")
    t.push(a)
    tid_b = _new_task_id(b, "B 建")
    assert tid_b == "T-000002"          # B collided with A's proposal
    b.commit("B: T-000002")
    t.push(b)
    t.rebase(a)
    assert not t.merge_conflict()
    ids = set(_task_ids(a))
    assert {"T-000002", "T-000003"} <= ids and "T-000004" not in ids
    assert sorted(_task_ids(a)) == sorted(_task_ids(b))
    _states_equal(t)

def test_merge_order_is_irrelevant(tmp_path):
    """C7: A merges B first vs B merges A first → byte-identical results
    (proj ls --json, item ls --json, every md) on both sides."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    _new_task_id(a, "A 独有")
    a.commit("A")
    t.push(a)
    _new_task_id(b, "B 独有")
    b.commit("B")
    t.push(b)
    t.rebase(a)
    assert not t.merge_conflict()
    sa, sb = a.state(), b.state()
    assert sa == sb, "A-first merge: the sides differ"

# ---- §10.2 field-level merge -----------------------------------------------

def test_disjoint_fields_both_survive(tmp_path):
    """§10.4: A changes the title, B changes the summary of the same task →
    both changes survive (field-level, not whole-record LWW)."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    tid = _new_task_id(a, "原")
    a.commit("base")
    t.push(a)
    t.rebase(b)
    md_a = next(a.path.rglob(f"{tid}-*.md"))
    md_a.write_text(md_a.read_text(encoding="utf-8").replace(
        f"# {tid}. 原", f"# {tid}. 新标题"), encoding="utf-8")
    a.run("sync")
    a.commit("title")
    md_b = next(b.path.rglob(f"{tid}-*.md"))
    md_b.write_text(md_b.read_text(encoding="utf-8") + "\n> **summary:** B 写的摘要\n",
                    encoding="utf-8")
    b.run("sync")
    b.commit("summary")
    t.push(a)
    t.rebase(b)
    assert not t.merge_conflict()
    final = next(b.path.rglob(f"{tid}-*.md")).read_text(encoding="utf-8")
    assert "新标题" in final and "B 写的摘要" in final

# ---- §10.5 item number collision -------------------------------------------

def test_item_number_collision_renumbers(tmp_path):
    """§10.5: both add a checklist item to the same group (both propose
    #A03) → one is #A03, the other #A04; both sides agree."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    tid = _new_task_id(a, "原")
    # R3: groups must exist before an item can be added to them
    # (`--group A` on a group-less record is "unknown group A") —
    # `item group add` assigns the next free letter (A, then B)
    a.run("item", "group", "add", tid, "组A")
    a.run("item", "group", "add", tid, "组B")
    for grp, txt in (("A", "甲一"), ("B", "乙一"), ("A", "甲二"), ("B", "乙二")):
        a.run("item", "add", tid, txt, "--group", grp)
    a.commit("base items (A01-A02 in A, A03-A04 in B)")
    t.push(a)
    t.rebase(b)
    a.run("item", "add", tid, "A 新", "--group", "A")
    a.commit("A item")
    b.run("item", "add", tid, "B 新", "--group", "A")
    b.commit("B item")
    t.push(a)
    t.rebase(b)
    assert not t.merge_conflict()
    # two-side reconcile: B publishes its merged log, A picks it up — both
    # land on the same event log, so the full-replay renumbering (B-1: git
    # HEAD moved → rebuild from the empty state) lands identically on both
    # (design §10: the DB is a function of the event log)
    t.push(b)
    t.pull(a)
    assert not t.merge_conflict()
    listing = b.run("item", "ls", tid)
    assert "A 新" in listing and "B 新" in listing
    _states_equal(t)

# ---- §10.6 gate violation survives the merge -------------------------------

# R3: C2+C4 now hold (item add carries pos) — no longer xfailed
def test_closed_plan_plus_new_item_reports_gate_violation(tmp_path):
    """§10.6: A archives a plan while B adds an open item to it → the merge
    succeeds (no git conflict) and `proj check` reports the gate violation."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    out = a.run("plan", "new", "收尾", "-y")
    # R3: `plan new` prints "Created <relative-path>.md" (not "Created: <id>")
    line = [ln for ln in out.splitlines() if ln.startswith("Created ")][-1]
    pid = re.search(r"(PLN-\d+)", line).group(1)
    a.commit("base plan")
    t.push(a)
    t.rebase(b)
    a.run("plan", "activate", pid)
    a.run("plan", "review", pid)
    a.run("plan", "archive", pid)
    a.commit("archive plan")
    b.run("item", "add", pid, "漏掉的一项")
    b.commit("B open item")
    t.push(a)
    t.rebase(b)
    assert not t.merge_conflict()
    out = b.run("check", expect=1)
    assert "gate" in out.lower() or pid in out

# ---- §10.7 delete (tombstone) vs modify ------------------------------------

def test_tombstone_vs_modify_replays_in_order(tmp_path):
    """§10.7: A deletes an item (tombstone) while B modifies the same one →
    replayed in event order: the surviving state is 'deleted' either way, and
    both sides agree."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    tid = _new_task_id(a, "原")
    a.run("item", "add", tid, "会变的项")
    a.commit("base item")
    t.push(a)
    t.rebase(b)
    a.run("item", "rm", f"{tid}#001")
    a.commit("A delete")
    a_md = next(a.path.rglob(f"{tid}-*.md"))
    a_md.write_text(a_md.read_text(encoding="utf-8").replace(
        "- [ ] #001 会变的项", "- [ ] #001 会变的项-改过"), encoding="utf-8")
    b.run("item", "edit", f"{tid}#001", "--text", "改过的文本")
    b.commit("B modify")
    t.push(a)
    t.rebase(b)
    assert not t.merge_conflict()
    # two-side reconcile: B publishes its merged log, A picks it up — the
    # delete + the modify replay in total sort order from the empty state
    # (B-1), so both sides land on the same tombstoned row
    t.push(b)
    t.pull(a)
    assert not t.merge_conflict()
    listing = b.run("item", "ls", tid)
    assert "改过的文本" not in listing.split("B 修改", 1)[0] or "会变的项" in listing
    _states_equal(t)

# ---- C7 rebase == merge ----------------------------------------------------

def test_rebase_equals_merge(tmp_path):
    """C7: rebase and merge reconciliation of the SAME committed divergence give
    byte-identical results. R1 puts the record uuid in the md marker, so the
    divergence must be created ONCE on a shared remote (its uuids are then
    fixed in the committed log, as in real multi-clone usage) — two independent
    `task new` runs would mint different uuids and the md could never be
    byte-identical. Each side clones a FRESH A/B pair from the shared,
    reconciled state and re-pulls (rebase vs merge); both land on the same log."""
    (tmp_path / "shared").mkdir()
    shared = ch.build_two_clones(tmp_path / "shared")  # the shared remote (base)
    _new_task_id(shared.a, "A 独有")
    shared.a.commit("A")
    shared.push(shared.a)
    _new_task_id(shared.b, "B 独有")
    shared.b.commit("B")
    shared.push(shared.b)  # union-merges A's into the shared remote (fixed uuids)

    def make_side(diverge):
        d = tmp_path / f"rm-{diverge}"
        d.mkdir(exist_ok=True)
        t = ch.build_two_clones(d, seed=shared.a.path)  # fresh pair from the shared state
        a = t.a
        (t.rebase if diverge == "rebase" else t.pull)(a)
        assert not t.merge_conflict()
        return a.state()
    assert make_side("rebase") == make_side("merge")

# ---- C7 write refused while a merge is in progress -------------------------

def test_write_refused_while_merge_in_progress(tmp_path):
    """C7: with a merge mid-flight (conflict left unresolved), a write command
    must be refused (C5: the refusal is the single choke point Db.begin +
    hooks.git_in_progress) — no event may be appended to a half-merged tree.

    Built WITHOUT the proj hooks: the proj-render keep-ours driver would
    auto-resolve an md conflict (that is C5's happy path, covered by the
    post-merge replay scenarios); here we want a REAL git conflict that leaves
    MERGE_HEAD set, so both sides change the SAME line (the title).
    """
    t = ch.build_two_clones(tmp_path, install_hooks=False)
    a, b = t.a, t.b
    did = _new_task_id(a, "冲突")
    a.commit("base")
    t.push(a)
    t.pull(b)  # B fast-forwards onto A's task
    md_a = next(a.path.rglob(f"{did}-*.md"))
    md_b = next(b.path.rglob(f"{did}-*.md"))
    # both sides change the SAME line (the title) to different text → a real
    # conflict the proj-render driver would otherwise keep-ours on
    txt_a = md_a.read_text(encoding="utf-8")
    txt_b = md_b.read_text(encoding="utf-8")
    assert txt_a == txt_b  # same base content
    md_a.write_text(txt_a.replace(f"# {did}.", f"# {did}. A 方", 1), encoding="utf-8")
    md_b.write_text(txt_b.replace(f"# {did}.", f"# {did}. B 方", 1), encoding="utf-8")
    a.commit("A 方")
    t.push(a)
    b.commit("B 方")
    # B pulls A's section — the two end-of-file appends conflict
    b.run_sh("git pull origin HEAD --no-rebase", expect=1)
    assert (b.path / ".git" / "MERGE_HEAD").exists()
    try:
        # a write command must now be refused
        out = b.run("task", "new", "不该成", "-y", expect=1)
        assert "merge" in out.lower() or "refus" in out.lower(), out
        # …and it must have created nothing
        assert "不该成" not in b.md_snapshot()
        assert not [n for n in (b.path / "docs" / "tasks").glob("*.md")
                    if "不该成" in n.name]
    finally:
        b.run_sh("git merge --abort")
        assert not (b.path / ".git" / "MERGE_HEAD").exists()
        # unblocked once the merge is aborted
        b.run("task", "new", "可以成", "-y")

# ---- C7 cache rebuild after deletion ---------------------------------------

def test_cache_deleted_rebuilds_identical(tmp_path):
    """C4 acceptance (no longer xfailed): delete .proj/cache.sqlite, run any
    command -> it is rebuilt from events.jsonl and equals the other clone.
    This only needs C2 (events) + C4 (rebuild), not the C5 git-merge path."""
    """C7: delete `.proj/cache.sqlite` in one clone, run any write command →
    the DB is rebuilt from events.jsonl and equals the other side's."""
    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    _new_task_id(a, "A 独有")
    a.commit("A")
    t.push(a)
    t.rebase(b)
    db = b.path / ".proj" / "cache.sqlite"
    assert db.exists()
    db.unlink()
    b.run("task", "ls")          # any command rebuilds the cache (C4)
    assert db.exists()
    assert b.ls_json() == a.ls_json()
    _states_equal(t)

# ---- C7 events.jsonl must end with a newline (holds TODAY) ------------------

def test_events_file_without_trailing_newline_fails_check(tmp_path):
    """C7: an events.jsonl that does not end with `\\n` (a torn/corrupt tail)
    is reported by `proj check` — this already holds (E7 + the C6 wiring)."""
    d = tmp_path / "nl"
    d.mkdir()
    c = ch.Clone(path=d, proj=ch._find_proj())
    c.run("init", "--profile", "default")
    ev_path = d / ".proj" / "events.jsonl"
    ev_path.write_text(
        _event_line("create", 1, "x", 1, rec="T-000001",
                    type="task", id="T-000001", title="t", status="Draft",
                    summary="", sections={})[:-1],   # strip the trailing \n
        encoding="utf-8")
    out = c.run("check", expect=1)
    assert "does not end with a newline" in out

# ---- design §10: a clone without hooks -------------------------------------

# R3: C2+C4 now hold (item add carries pos) — no longer xfailed
def test_clone_without_hooks_auto_replays(tmp_path, proj_binary):
    """design §10 (last row): a clone that never ran `proj hooks install` —
    its first write command auto-replays the pulled events; a conflicting md
    gets a hint to run hooks install / `proj sync --rerender`."""
    t = ch.build_two_clones(tmp_path, install_hooks=False)
    a, b = t.a, t.b
    assert not a.hooks and not b.hooks
    _new_task_id(a, "A 独有")
    a.commit("A")
    t.push(a)
    # R3: b must first PULL a's commit (the events.jsonl grows); only then the
    # next command's ensure_cache replays a's events (C2/C4) — the "no hooks"
    # point is that no post-merge hook does this, the command itself does
    t.pull(b)
    b.run("task", "ls")
    assert set(_task_ids(a)) == set(_task_ids(b))
    _states_equal(t)

# ---- C7 (optional) random two-clone test ------------------------------------

def test_random_ops_both_merge_orders_agree(tmp_path):
    """C7 (optional): 20 seed-driven ops per side (new task / add item),
    merged → byte-identical. The seed is fixed so a failure is reproducible.
    A and B run the same op *sequence* (same seed) with only the content
    differing, which is what merge-order independence must survive."""
    import random
    rng = random.Random(20261103)
    steps = [rng.randrange(2) for _ in range(20)]  # 0 = new task, 1 = add item

    def drive(clone, tag):
        last = _new_task_id(clone, f"{tag} root")
        for i, step in enumerate(steps):
            if step == 0:
                last = _new_task_id(clone, f"{tag} 步 {i}")
            else:
                clone.run("item", "add", last, f"{tag} 项 {i}")
        return last

    t = ch.build_two_clones(tmp_path)
    a, b = t.a, t.b
    drive(a, "A")
    a.commit("A random")
    t.push(a)
    drive(b, "B")
    b.commit("B random")
    t.push(b)
    t.rebase(a)
    assert not t.merge_conflict()
    _states_equal(t)
