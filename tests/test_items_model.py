"""Pure-function tests for engine.items (no project needed — fast).

Covers plans/items-design.md §8.2: norm_no/group_of leniency, next_no/
next_group skipping tombstone numbers, parse → apply_md → render roundtrips
(items, groups, trailers, intro, note/owner/due), cross-group renumbering,
no reuse, group-letter reuse, capacities, the problem set, diff_log outputs,
and set_state's no-change rule.
"""
import pytest

from proj.core.templates import ItemsSpec, ItemState
from proj.engine import items as it

# a task-style checklist: one todo state, three done states (two with emoji)
TASK = ItemsSpec(
    section="Acceptance Criteria",
    states=[ItemState("todo"), ItemState("done", "✅", closed=True),
            ItemState("met", "✅", closed=True),
            ItemState("skipped", "⏭️", closed=True)],
    fields=["owner", "due"])
PLAN = ItemsSpec(
    section="Success Criteria",
    states=[ItemState("pending"),
            ItemState("met", "✅", closed=True),
            ItemState("missed", "❌", closed=True)])


def _state(rows=()):
    return it.ItemsState(rows=list(rows))


# ---- numbers ---------------------------------------------------------------

@pytest.mark.parametrize("s, want", [
    ("1", "001"), ("001", "001"), ("999", "999"),
    ("a1", "A01"), ("A01", "A01"), ("a12", "A12"), ("A99", "A99"),
    ("a", "A00"), ("A0", "A00"), (" z", "Z00"),
])
def test_norm_no(s, want):
    assert it.norm_no(s) == want


@pytest.mark.parametrize("s", ["", "1000", "A100", "A999", "1a", "aa1"])
def test_norm_no_bad(s):
    with pytest.raises(it.ItemsError):
        it.norm_no(s)


def test_group_of():
    assert it.group_of("A01") == "A"
    assert it.group_of("001") == ""


def test_next_no_skips_tombstones():
    st = _state([it.Item("001"), it.Item("003"), it.Item("003", deleted=True)])
    assert st.next_no("") == "004"
    g = it.ItemsState(rows=[it.Item("A00", text="G"), it.Item("A01"),
                            it.Item("A02", deleted=True)])
    assert g.next_no("A") == "A03"


def test_next_group_skips_tombstones():
    st = _state([it.Item("A00", text="A"), it.Item("B00", text="B", deleted=True)])
    assert st.next_group() == "C"


def test_capacity_limits():
    st = it.ItemsState(rows=[it.Item(f"{i:03d}") for i in range(1, 1000)])
    with pytest.raises(it.ItemsError, match="capacity"):
        st.next_no("")
    st = it.ItemsState(rows=[it.Item("A00", text="G")] +
                       [it.Item(f"A{i:02d}") for i in range(1, 100)])
    with pytest.raises(it.ItemsError, match="capacity"):
        st.next_no("A")
    st = it.ItemsState(rows=[it.Item(f"{L}00", text=L) for L in it.LETTERS])
    with pytest.raises(it.ItemsError, match="groups"):
        st.next_group()


# ---- parse → apply_md → render roundtrips -----------------------------------

MD_PLAIN = """intro line
- [ ] #001 a
- [x] #002 ✅ b
"""


def _state_from(md, spec):
    """A state matching an already-numbered section (sync steady state)."""
    p = it.parse_section(md, spec)
    st = it.ItemsState(intro=p.intro)
    for i in p.ungrouped:
        st.rows.append(it.Item(no=i.no, text=i.text, state=i.state,
                               note=i.note, owner=i.owner, due=i.due,
                               trailer=i.trailer))
    for g in p.groups:
        st.rows.append(it.Item(no=g.letter + "00", text=g.title,
                               trailer=g.trailer))
        for i in g.items:
            st.rows.append(it.Item(no=i.no, text=i.text, state=i.state,
                                   note=i.note, owner=i.owner, due=i.due,
                                   trailer=i.trailer))
    return st


def test_roundtrip_plain():
    st = _state_from(MD_PLAIN, TASK)
    new, _ = it.apply_md(st, it.parse_section(MD_PLAIN, TASK), TASK)
    rendered = it.render_section(new, TASK)
    assert it.normalize(MD_PLAIN) == rendered
    again, _ = it.apply_md(new, it.parse_section(rendered, TASK), TASK)
    assert it.render_section(again, TASK) == rendered
    assert [r.no for r in new.live()] == ["001", "002"]
    assert new.by_no("002").state == "done"


def test_numbered_line_on_empty_state_is_problem():
    st, _ = it.apply_md(_state(),
                        it.parse_section("- [ ] a\n", TASK), TASK)
    with pytest.raises(it.ItemsError, match="unknown item number"):
        it.apply_md(st, it.parse_section(MD_PLAIN, TASK), TASK)


def test_roundtrip_numbers_groups_trailers():
    md = ("intro line\n\n- [ ] #001 a\ntrailer under one\n\n"
          "### A · Backend\n- [x] #A01 ✅ b\n"
          "- [ ] #A02 c · owner: o · due: 2026-11-15\n\n"
          "### B · Frontend\n")
    st = _state_from(md, TASK)
    new, _ = it.apply_md(st, it.parse_section(md, TASK), TASK)
    rendered = it.render_section(new, TASK)
    again, _ = it.apply_md(new, it.parse_section(rendered, TASK), TASK)
    assert it.render_section(again, TASK) == rendered
    assert new.by_no("001").trailer == "trailer under one"
    assert new.by_no("A02").owner == "o"
    assert new.by_no("A02").due == "2026-11-15"
    assert new.by_no("A00").is_group and new.by_no("A00").text == "Backend"
    assert new.by_no("B00").is_group  # an empty group survives


def test_unknown_heading_gets_letter():
    new, notes = it.apply_md(_state(), it.parse_section("### Backend\n- [ ] x\n", PLAN), PLAN)
    assert new.by_no("A00").text == "Backend"
    assert new.by_no("A01").text == "x"
    assert any("group A" in n for n in notes)


def test_emoji_conflict_is_problem():
    p = it.parse_section("- [ ] #001 ❌ x\n", PLAN)
    assert any("checkbox" in x for x in p.problems)


def test_checked_without_emoji_assigns_note():
    p = it.parse_section("- [x] #001 x\n", PLAN)
    assert p.ungrouped[0].state == "met"
    assert p.ungrouped[0].note == it.NOTE_ASSIGNED


@pytest.mark.parametrize("md, frag", [
    ("  - [ ] #001 x\n", "nest"),                       # indented → problem
    ("- [ ] x\n- [ ] #001 a\n- [ ] #001 b\n", "duplicate"),
    ("### Q · nope\n- [ ] x\n", "unknown group"),      # unknown letter
    ("- [ ] #999 a\n- [ ] b\n", "unknown item"),       # via apply_md
])
def test_problems(md, frag):
    p = it.parse_section(md, PLAN)
    try:
        it.apply_md(_state(), p, PLAN)
    except it.ItemsError as e:
        assert frag in str(e)
        return
    assert any(frag in x for x in p.problems)


# R1: moving a numbered line under another heading in the md renumbers it —
# the moved row must stay LIVE under its fresh number (it was tombstoned
# because the "missing" pass compared the OLD number), whether or not another
# group follows the target and whether or not tombstones already exist.
_MOVE_BASE = [it.Item("001", text="one"), it.Item("A00", text="G"),
              it.Item("A01", text="g1"), it.Item("B00", text="H")]


@pytest.mark.parametrize("rows, md, want", [
    # target group A is followed by group B (was: "tombstones must come last")
    (_MOVE_BASE,
     "### A · G\n- [ ] #A01 g1\n- [ ] #001 one\n\n### B · H\n", "A02"),
    # target group is the last one (was: the moved item silently tombstoned)
    (_MOVE_BASE,
     "### A · G\n- [ ] #A01 g1\n\n### B · H\n- [ ] #001 one\n", "B01"),
    # with an existing tombstone
    ([*_MOVE_BASE, it.Item("002", text="old", deleted=True)],
     "### A · G\n- [ ] #A01 g1\n- [ ] #001 one\n\n### B · H\n", "A02"),
    # into a group created by a new heading in the same edit
    (_MOVE_BASE[:3],
     "### A · G\n- [ ] #A01 g1\n\n### New\n- [ ] #001 one\n", "B01"),
])
def test_md_cross_group_move_keeps_item_live(rows, md, want):
    st = _state([it.Item(r.no, text=r.text, deleted=r.deleted) for r in rows])
    new, notes = it.apply_md(st, it.parse_section(md, PLAN), PLAN)
    moved = new.by_no(want)
    assert moved is not None and not moved.deleted and moved.text == "one"
    assert new.by_no("001").deleted                       # old number: tombstone
    assert [r.no for r in new.live() if not r.is_group].count(want) == 1
    assert f"item #001 → #{want}" in notes
    # the render shows it, and re-applying the render is a no-op
    out = it.render_section(new, PLAN)
    assert f"#{want} one" in out
    again, _ = it.apply_md(new, it.parse_section(out, PLAN), PLAN)
    assert [r.to_row() for r in again.rows] == [r.to_row() for r in new.rows]


# ---- transforms ------------------------------------------------------------

def test_add_group_after_first():
    st = _state([it.Item("001", text="a"), it.Item("002", text="b")])
    new, _ = it.add(st, PLAN, "c", after="1")
    assert [r.no for r in new.live()] == ["001", "003", "002"]
    new, _ = it.add(st, PLAN, "c", after="2")
    assert [r.no for r in new.live()] == ["001", "002", "003"]
    with pytest.raises(it.ItemsError, match="share the target group"):
        it.add(_state([it.Item("001", text="x"), it.Item("A00", text="G"),
                       it.Item("A01", text="a")]),
                PLAN, "c", after="1", group="A")


def test_set_state_no_change_no_notes():
    st = _state([it.Item("001", text="a", state="met")])
    new, notes = it.set_state(st, PLAN, "001", "met")
    assert notes == []
    assert new is not st and [r.state for r in new.rows] == ["met"]
    new, notes = it.set_state(st, PLAN, "001", "missed")
    assert new.by_no("001").closed_at
    assert notes == ["item #001 missed"]


def test_edit_clears():
    st = _state([it.Item("001", text="a", owner="o")])
    new, notes = it.edit(st, TASK, "001", owner="")
    assert new.by_no("001").owner == "" and notes == ["item #001 edited"]
    new, notes = it.edit(st, TASK, "001", owner="o")
    assert notes == []
    with pytest.raises(it.ItemsError, match="owner is not"):
        it.edit(st, PLAN, "001", owner="x")


def test_cross_group_move_renumbers_no_reuse():
    st = _state([it.Item("A00", text="G"), it.Item("A01", text="a"),
                 it.Item("001", text="x")])
    new, notes = it.move(st, PLAN, "001", group="A")
    assert new.by_no("001").deleted           # the old number is a tombstone
    assert not new.by_no("A02").deleted and new.by_no("A02").text == "x"
    assert "item #001 → #A02" in notes
    # adding again takes A03 (A01, A02 live; nothing reused)
    later, _ = it.add(new, PLAN, "y")
    assert later.by_no("002") is not None


def test_remove_then_no_reuse():
    st = _state([it.Item("A00", text="G"), it.Item("A01", text="a"),
                 it.Item("A02", text="b")])
    new, _ = it.remove(st, PLAN, "A02")
    later, _ = it.add(new, PLAN, "c", group="A")
    assert later.by_no("A03") is not None


def test_group_rm_letters_never_reused():
    st = _state([it.Item("A00", text="A"), it.Item("B00", text="B")])
    new, _ = it.group_rm(st, PLAN, "B")
    later, _ = it.group_add(new, PLAN, "C")
    assert later.by_no("C00").text == "C"     # not B (tombstoned)


def test_group_rm_nonempty_needs_choice():
    st = _state([it.Item("A00", text="G"), it.Item("A01", text="a")])
    with pytest.raises(it.ItemsError, match="ungroup"):
        it.group_rm(st, PLAN, "A")
    new, _ = it.group_rm(st, PLAN, "A", "ungroup")
    assert new.by_no("A00").deleted
    assert not new.by_no("001").deleted and new.by_no("001").text == "a"


# ---- diff_log ---------------------------------------------------------------

def test_diff_log_outputs():
    old = _state([it.Item("001", text="a"), it.Item("002", text="b"),
                  it.Item("A00", text="G")])
    new = _state([it.Item("001", text="a", state="met"),
                  it.Item("002", text="b", deleted=True),
                  it.Item("A00", text="G2"),
                  it.Item("B00", text="H")])
    out = it.diff_log(old, new, [])
    assert ("Added", "group B · H") in out
    assert ("Removed", "item #002: b") in out
    assert ("Changed", "item #001  → met") in out or any(
        c == "Changed" and "001" in m for c, m in out)
    assert ("Changed", "group A renamed") in out


def test_diff_log_move_one_line():
    old = _state([it.Item("001", text="a")])
    new = _state([it.Item("001", text="a", deleted=True),
                  it.Item("A01", text="a")])
    out = it.diff_log(old, new, ["item #001 → #A01"])
    assert out == [("Changed", "item #001 → #A01")]
