"""Pure model tests for engine/items.py (plans/items-design.md §8 step 2):
parse_no, parse_section → apply_md → render_section roundtrips, cross-group
moves, never-reuse, capacities, problems, diff_log, the set_state no-op.
No project fixture — these run in milliseconds."""
from proj.core.templates import ItemsSpec, ItemState
from proj.engine import items as it

# task-like: one open state, one closed state → closed state shows NO emoji
TASK = ItemsSpec(
    section="Acceptance Criteria", noun="criterion",
    states=[ItemState(name="todo"), ItemState(name="done", closed=True)],
    fields=["owner", "due"])

# plan-like: one open, two closed states with distinct emojis → emoji shown
PLAN = ItemsSpec(
    section="Success Criteria", noun="criterion",
    states=[ItemState(name="pending"),
            ItemState(name="met", emoji="✅", closed=True),
            ItemState(name="missed", emoji="❌", closed=True)],
    fields=["owner", "due"])


def _state():
    return it.ItemsState(intro="")


# ---- parse_no / ItemNo -------------------------------------------------------

def test_parse_no_roundtrip():
    assert it.parse_no("1") == it.ItemNo("", 1)
    assert str(it.parse_no("1")) == "001"
    assert it.parse_no("001") == it.ItemNo("", 1)
    assert it.parse_no("a1") == it.ItemNo("A", 1)
    assert str(it.parse_no("a1")) == "A01"
    assert it.parse_no("B99") == it.ItemNo("B", 99)
    assert str(it.ItemNo("", 999)) == "999"
    assert str(it.ItemNo("Z", 1)) == "Z01"


def test_parse_no_rejects():
    for bad in ("A100", "1000", "x", "", "01A", "AA01"):
        try:
            it.parse_no(bad)
            raise AssertionError(f"{bad!r} should not parse")
        except ValueError:
            pass


# ---- parse → apply → render roundtrip ----------------------------------------

MD = """intro line

- [ ] error rate below 0.1%
- [x] 3 users onboarded — five users, see PR #88 · owner: o · due: 2026-12-31
trailer under item two

### Backend
group trailer here
- [x] API returns 200
### Frontend
- [ ] form validates"""


def test_roundtrip_numbers_groups_trailers():
    new, notes = it.apply_md(_state(), it.parse_section(MD, PLAN), PLAN)
    assert [str(i.no) for i in new.items] == ["001", "002", "A01", "B01"]
    assert [g.letter for g in new.groups] == ["A", "B"]
    assert new.last_seq == {"": 2, "A": 1, "B": 1}
    assert new.last_group == 2
    assert any("without state emoji → met" in n for n in notes)
    rendered = it.render_section(new, PLAN)
    for line in ["intro line", "- [ ] #001 error rate below 0.1%",
                 "- [x] #002 ✅ 3 users onboarded — five users, see PR #88 "
                 "· owner: o · due: 2026-12-31",
                 "trailer under item two", "### A · Backend", "group trailer here",
                 "- [x] #A01 ✅ API returns 200", "### B · Frontend",
                 "- [ ] #B01 form validates"]:
        assert line in rendered, line
    # parse the render back: identical state, byte-identical render
    again, _ = it.apply_md(new, it.parse_section(rendered, PLAN), PLAN)
    assert again == new
    assert it.render_section(again, PLAN) == rendered


def test_roundtrip_single_closed_state_has_no_emoji():
    new, _ = it.apply_md(_state(), it.parse_section("- [ ] a\n- [x] b", TASK), TASK)
    rendered = it.render_section(new, TASK)
    assert "- [x] #002 b" in rendered and "✅" not in rendered


def test_apply_rejects_unknown_number_and_duplicate():
    new, _ = it.apply_md(_state(), it.parse_section("- [ ] a\n- [ ] b", TASK), TASK)
    p = it.parse_section("- [x] #001 a\n- [ ] #099 ghost", TASK)
    try:
        it.apply_md(new, p, TASK)
        raise AssertionError("unknown number must raise")
    except it.ItemsError as e:
        assert "does not exist" in str(e)
    p = it.parse_section("- [x] #001 a\n- [ ] #001 dup", TASK)
    try:
        it.apply_md(new, p, TASK)
        raise AssertionError("duplicate number must raise")
    except it.ItemsError as e:
        assert "duplicate" in str(e)


def test_apply_rejects_unknown_and_double_group_heading():
    new, _ = it.apply_md(_state(), it.parse_section(MD, PLAN), PLAN)
    p = it.parse_section("- [x] #001 a\n\n### Q · nope\n- [ ] b", PLAN)
    try:
        it.apply_md(new, p, PLAN)
        raise AssertionError("unknown group letter must raise")
    except it.ItemsError as e:
        assert "unknown group" in str(e)
    p = it.parse_section("- [x] #A01 a\n\n### A · x\n- [ ] b\n### A · y\n- [ ] c", PLAN)
    try:
        it.apply_md(new, p, PLAN)
        raise AssertionError("double heading must raise")
    except it.ItemsError as e:
        assert "duplicate group" in str(e)


def test_parse_reports_indent_conflict_and_disabled_field():
    p = it.parse_section("- [ ] a\n    - [ ] deep", TASK)
    assert any("no longer nest" in x for x in p.problems)
    p = it.parse_section("- [ ] #001 ✅ c", PLAN)  # met is closed, box is open
    assert any("emoji and checkbox conflict" in x for x in p.problems)
    bare = ItemsSpec(section="X", noun="criterion", states=[ItemState(name="a")])
    p = it.parse_section("- [ ] c · owner: x", bare)  # no owner field enabled
    assert any("not an enabled field" in x for x in p.problems)


# ---- cross-group move + never-reuse ------------------------------------------

def _two_plus_a():
    s = _state()
    s.groups.append(it.Group("A", "A"))
    s.last_group = 1
    s.last_seq = {"": 2, "A": 1}
    s.items = [it.Item(no=it.ItemNo("", 1), text="one", state="todo"),
               it.Item(no=it.ItemNo("", 2), text="two", state="todo"),
               it.Item(no=it.ItemNo("A", 1), text="a one", state="done",
                       closed_at="2026-01-01")]
    s.check()
    return s


def test_cross_group_move_gets_fresh_number():
    s = _two_plus_a()
    md = """- [x] #001 one

### A · A
- [ ] #002 two
- [x] #A01 a one"""
    new, notes = it.apply_md(s, it.parse_section(md, TASK), TASK)
    assert [str(i.no) for i in new.items] == ["001", "A02", "A01"]
    assert any("#002 → #A02" in n for n in notes)
    assert new.last_seq["A"] == 2
    moved = new.items[1]
    assert moved.text == "two" and moved.closed_at == ""  # fields preserved
    assert any(cat == "Changed" and "→" in msg for cat, msg
               in it.diff_log(s, new, notes))


def test_delete_then_add_never_reuses_number():
    s = _two_plus_a()
    new, _ = it.apply_md(s, it.parse_section("- [x] #001 one\n\n### A · A\n- [x] #A01 a one",
                                             TASK), TASK)
    # #002 deleted (number 2 still issued): a new item must be #003
    added, _ = it.add(new, TASK, "late")
    assert str(next(i for i in added.items if i.text == "late").no) == "003"
    # group B was never allocated, C would be next if B were deleted:
    s2 = _two_plus_a()
    s2.groups.append(it.Group("B", "B"))
    s2.last_group = 2
    md = ("- [x] #001 one\n\n### A · A\n- [x] #A01 a one\n### Later\n- [ ] z")
    new2, _ = it.apply_md(s2, it.parse_section(md, TASK), TASK)
    assert [g.letter for g in new2.groups] == ["A", "C"]
    assert new2.last_group == 3


def test_deleting_heading_rehomes_and_deletes_group():
    s = _two_plus_a()
    new, _ = it.apply_md(s, it.parse_section("- [x] #001 one\n- [x] #A01 a one",
                                             TASK), TASK)
    # group A disappeared; #A01 re-homed to ungrouped as a fresh number
    # (the ungrouped counter had already issued 2 → it becomes #003)
    assert [str(i.no) for i in new.items] == ["001", "003"]
    assert new.groups == []


def test_capacity_limits():
    full = _state()
    full.last_seq[""] = it.UNGROUPED_CAP
    full.items = [it.Item(no=it.ItemNo("", 1), text="x", state="todo")]
    full.check()
    try:
        it.apply_md(full, it.parse_section("- [x] #001 x\n- [ ] more", TASK), TASK)
        raise AssertionError("1000th ungrouped item must raise")
    except it.ItemsError as e:
        assert "full" in str(e)
    grp_full = _two_plus_a()
    grp_full.last_seq["A"] = it.GROUP_CAP
    grp_full.check()
    try:
        md = "- [x] #001 one\n\n### A · A\n- [ ] #A01 a one\n- [ ] more"
        it.apply_md(grp_full, it.parse_section(md, TASK), TASK)
        raise AssertionError("100th group item must raise")
    except it.ItemsError as e:
        assert "full" in str(e)
    many = _state()
    many.last_group = len(it.GROUP_LETTERS)
    many.check()
    try:
        it.apply_md(many, it.parse_section("### one more\n- [ ] x", TASK), TASK)
        raise AssertionError("27th group must raise")
    except it.ItemsError as e:
        assert "cap" in str(e)


# ---- ops transforms + diff_log ------------------------------------------------

def test_add_after_and_group():
    s = _two_plus_a()
    new, _ = it.add(s, TASK, "sub", after=it.ItemNo("", 1))
    assert [str(i.no) for i in new.items] == ["001", "003", "002", "A01"]
    try:
        it.add(s, TASK, "x", group="A", after=it.ItemNo("", 2))
        raise AssertionError("after must be in the target group")
    except it.ItemsError:
        pass
    new, _ = it.add(s, TASK, "a two", group="A")
    assert str(new.items[-1].no) == "A02"


def test_set_state_noop_and_changelog():
    s = _two_plus_a()
    new, notes = it.set_state(s, TASK, it.ItemNo("", 1), "todo")
    assert new == s and notes == []
    assert it.diff_log(s, new, notes) == []
    new, _ = it.set_state(s, TASK, it.ItemNo("", 1), "done")
    log = it.diff_log(s, new, [])
    assert ("Changed", "item #001 todo → done") in log
    assert new.items[0].closed_at != ""
    re = it.set_state(new, TASK, it.ItemNo("", 1), "done")
    assert it.diff_log(new, re[0], []) == []


def test_diff_log_every_kind():
    s = _two_plus_a()
    new, notes = it.remove(s, TASK, it.ItemNo("A", 1))
    added, notes = it.add(new, TASK, "fresh")
    renamed, _ = it.group_rename(added, TASK, "A", "Backend")
    mv, mnotes = it.move(renamed, TASK, it.ItemNo("", 2), group="A")
    log = it.diff_log(s, mv, mnotes + notes)
    assert ("Removed", "item #A01: a one") in log
    assert ("Added", "item #003: fresh") in log
    assert ("Changed", "group A renamed") in log
    assert ("Changed", "item #002 → #A02") in log
    # edit: text change logs 'edited'
    ed, _ = it.edit(s, TASK, it.ItemNo("", 1), text="renamed")
    assert ("Changed", "item #001 edited") in it.diff_log(s, ed, [])
    # group add / remove
    g, _ = it.group_add(s, TASK, "B")
    assert ("Added", "group B · B") in it.diff_log(s, g, [])
    gr, _ = it.group_rm(g, TASK, "B", items="delete")
    assert ("Removed", "group B") in it.diff_log(g, gr, [])


def test_edit_clears_and_validates():
    s = _two_plus_a()
    item = s.items[0]
    full = it.Item(no=item.no, text="t", state="todo", note="n", owner="o", due="2026-12-31")
    s.items[0] = full
    s.check()
    new, _ = it.edit(s, TASK, it.ItemNo("", 1), text="x", note="", owner="", due="")
    assert new.items[0].note == "" and new.items[0].owner == "" and new.items[0].due == ""
    for kw, frag in (({"due": "not-a-date"}, "valid date"),
                     ({"text": "a — b"}, "may not contain")):
        try:
            it.edit(s, TASK, it.ItemNo("", 1), **kw)
            raise AssertionError(f"{kw} must raise")
        except it.ItemsError as e:
            assert frag in str(e)
    no_owner = ItemsSpec(section="X", noun="criterion",
                         states=[ItemState(name="a")])
    try:
        it.edit(s, no_owner, it.ItemNo("", 1), owner="x")  # no owner field
        raise AssertionError("no owner field must raise")
    except it.ItemsError as e:
        assert "no owner field" in str(e)


def test_move_requires_exactly_one_target():
    s = _two_plus_a()
    for kw in ({"group": "A", "first": True}, {"after": it.ItemNo("", 1)}):
        if "after" in kw and "group" not in kw:
            pass  # 'after' alone is fine
            new, _ = it.move(s, TASK, it.ItemNo("", 2), **kw)
            assert new is not None
            continue
        try:
            it.move(s, TASK, it.ItemNo("", 1), **kw)
            raise AssertionError(f"{kw} must raise")
        except it.ItemsError as e:
            assert "exactly one" in str(e)
