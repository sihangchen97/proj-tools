import pytest

from proj.tasklist import add_task, list_plans, new_plan, read_tasks, resume_plan, shelve_task

ACTIVE = """\
# Tasklist

## Big feature

- [x] first step
- [ ] second step
"""


def test_new_plan(cfg):
    p = new_plan(cfg, "Overall roadmap")
    assert p.is_file()
    assert p.parent == cfg.plans_dir
    text = p.read_text()
    assert "# Overall roadmap" in text
    assert "- [ ] break into atomic" in text


def test_shelve_parks_incomplete_task(cfg):
    add_task(cfg, "Big feature")
    (cfg.root / "TASKLIST.md").write_text(ACTIVE)
    p = shelve_task(cfg, "Big feature")
    assert p.is_file()
    text = p.read_text()
    assert "Shelved" in text
    assert "(1/2 done)" in text
    assert read_tasks(cfg)[1] == []


def test_resume_brings_plan_back(cfg):
    add_task(cfg, "Big feature")
    (cfg.root / "TASKLIST.md").write_text(ACTIVE)
    p = shelve_task(cfg, "Big feature")
    resume_plan(cfg, "Big feature")
    assert not p.exists()
    _h, blocks = read_tasks(cfg)
    assert len(blocks) == 1
    assert blocks[0][0] == "Big feature"
    assert blocks[0][2].progress == "1/2"


def test_resume_active_conflict(cfg):
    add_task(cfg, "Big feature")
    (cfg.root / "TASKLIST.md").write_text(ACTIVE)
    new_plan(cfg, "Big feature")  # same title as active task
    with pytest.raises(ValueError, match="already active"):
        resume_plan(cfg, "Big feature")


def test_resume_missing(cfg):
    with pytest.raises(ValueError):
        resume_plan(cfg, "Nope")


def test_list_plans_newest_first(cfg):
    new_plan(cfg, "Alpha")
    new_plan(cfg, "Beta")
    plans = list_plans(cfg)
    assert [t for _, t, _ in plans] == ["Beta", "Alpha"]
