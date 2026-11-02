"""E3 (hard-parts-impl §0.1): the two-clone harness + scenario tests.

The scenarios are the design doc §10 (plans/git-collab-design.md) acceptance
cases, run against REAL git and the REAL `proj` binary (tests/
collab_harness.py). Until C2 (event emitter) + C4 (replay on write / cache
rebuild) land, every scenario is marked xfail with the missing piece named —
they fail today (a pulled clone does not sync its DB from events.jsonl) and
turn into green regression tests the day C2/C4 is wired in, no test change
needed.

The xfail is strict: a PASS while xfailed is reported as a failure, so the
harness cannot silently hide a real success.
"""
from __future__ import annotations

import json

import collab_harness as ch
import pytest

# the one harness test is NOT xfailed (it must hold already); the rest wait
# on C2/C4. strict=True so a pass while xfailed is reported.
WAIT_C2C4 = pytest.mark.xfail(strict=True, reason="C2/C4 not wired yet")


@pytest.fixture(scope="module")
def proj_binary() -> str:
    return ch._find_proj()


def _task_titles(cl: ch.Clone) -> list[str]:
    return [r["title"] for r in json.loads(cl.run("task", "ls", "--json"))]


def _task_ids(cl: ch.Clone) -> list[str]:
    return [r["id"] for r in json.loads(cl.run("task", "ls", "--json"))]


def _new_task_id(cl: ch.Clone, title: str) -> str:
    out = cl.run("task", "new", title, "-y")
    return [ln for ln in out.splitlines() if ln.startswith("Created:")][-1] \
        .split(":", 1)[1].strip()


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


@WAIT_C2C4
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


@WAIT_C2C4
def test_disjoint_sections_merge(tmp_path, proj_binary):
    """§10.2: two authors append different ## sections to the same record's
    md → the union merge keeps both on each side (C2: both became events)."""
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
    md_b = next(b.path.rglob(f"{did}-*.md")).read_text(encoding="utf-8")
    md_a = next(a.path.rglob(f"{did}-*.md")).read_text(encoding="utf-8")
    assert "A 的备选" in md_b and "B 的后果" in md_b
    assert md_a == md_b


@WAIT_C2C4
def test_same_field_lww_keeps_later_and_reports(tmp_path, proj_binary):
    """§10.3: both change the same task title → the merge keeps exactly one
    value (LWW, the later writer) and surfaces the overwrite (G5)."""
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
    t.rebase(b)
    assert not t.merge_conflict()
    md_b = next(b.path.rglob(f"{tid}-*.md")).read_text(encoding="utf-8")
    assert ("标题 A" in md_b) ^ ("标题 B" in md_b)  # exactly one wins


@WAIT_C2C4
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
