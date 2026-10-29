from proj import decision as dec
from proj.check import check
from proj.scaffold import init
from proj.tasklist import add_task


def test_clean_project_passes(cfg):
    init(cfg, ["core"])
    assert check(cfg) == []


def test_bad_status_fails(cfg):
    init(cfg, ["core"])
    rec = dec._records(cfg)[0]
    # Accepted is not in the template's status-allowed set for a fresh file? It is.
    # Use an out-of-set status instead:
    rec.meta["status"] = "Whatever"
    rec.write()
    problems = check(cfg)
    assert any("status" in x and "Whatever" in x for x in problems)


def test_missing_uuid_fails(cfg):
    init(cfg, ["core"])
    rec = dec._records(cfg)[0]
    del rec.meta["uuid"]
    rec.write()
    problems = check(cfg)
    assert any("uuid" in x.lower() for x in problems)


def test_completed_task_fails(cfg):
    init(cfg, ["core"])
    add_task(cfg, "Done thing")
    (cfg.root / "TASKLIST.md").write_text(
        "# Tasklist\n\n## Done thing\n\n- [x] step\n")
    problems = check(cfg)
    assert any("proj task done" in x for x in problems)
