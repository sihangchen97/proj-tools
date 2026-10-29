import pytest

from proj.tasklist import add_task, done_task, read_tasks

DONE_BLOCK = """\
## Ship v1

- [x] write code
- [x] run tests
"""


def test_add_and_list(cfg):
    add_task(cfg, "Ship v1")
    _h, blocks = read_tasks(cfg)
    assert len(blocks) == 1
    assert blocks[0][0] == "Ship v1"
    with pytest.raises(ValueError):
        add_task(cfg, "Ship v1")


def test_done_requires_complete(cfg):
    add_task(cfg, "Ship v1")
    with pytest.raises(ValueError):
        done_task(cfg, "Ship v1")


def test_done_archives_and_removes(cfg):
    add_task(cfg, "Ship v1")
    (cfg.root / "TASKLIST.md").write_text(DONE_BLOCK)
    archive = done_task(cfg, "Ship v1")
    assert archive.is_file()
    assert archive.parent == cfg.plans_dir
    assert archive.name.startswith("2")
    _h, blocks = read_tasks(cfg)
    assert blocks == []


def test_done_missing(cfg):
    with pytest.raises(ValueError):
        done_task(cfg, "Nope")
