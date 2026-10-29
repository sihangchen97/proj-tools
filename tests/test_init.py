"""v3 init: scaffold, lock, seed, adoption, idempotence."""
from proj import db as dbmod
from proj import records as rec
from proj import scaffold


def test_init_creates_expected_files(proj):
    root = proj.root
    for expected in ["CHANGELOG.md", "TASKLIST.md", "CONTRIBUTING.md",
                     "templates/default/decision.yaml", "templates/default/task.yaml",
                     ".proj.lock", ".proj/.gitignore",
                     "docs/decisions/0001-record-architecture-decisions.md",
                     ".env.example", ".github/PULL_REQUEST_TEMPLATE.md"]:
        assert (root / expected).is_file(), expected
    assert proj.db_path.is_file()


def test_seed_decision_content(proj):
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    rows = db.all(spec)
    db.close()
    assert len(rows) == 1
    assert rows[0]["id"] == "0001"
    assert rows[0]["status"] == "Accepted"
    text = (proj.root / "docs/decisions/0001-record-architecture-decisions.md").read_text()
    assert "<!-- proj:decision " in text
    assert "Status: Accepted" in text
    assert "# 1. Record architecture decisions" in text


def test_lock_written_and_consistent(proj):
    lock = dbmod.read_lock(proj.root)
    assert lock is not None
    assert lock["schema_version"] == 1
    assert "decision" in lock["kinds"]
    # lock spec round-trips to the same schema
    specs = dbmod.specs_from_lock(lock)
    assert specs["decision"].status_allowed == \
        rec.load_registry(proj.root)["decision"].status_allowed


def test_init_idempotent(proj):
    # user-owned files are never recreated or overwritten
    (proj.root / "CONTRIBUTING.md").write_text("mine")
    created = scaffold.init(proj, ["core", "quality"])
    assert "CONTRIBUTING.md" not in created
    assert (proj.root / "CONTRIBUTING.md").read_text() == "mine"
    # rendered views get re-rendered (they are generated, not user-owned)
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    assert len(db.all(spec)) == 1  # no duplicate seed
    db.close()


def test_task_kind_registered(proj):
    reg = rec.load_registry(proj.root)
    assert reg["task"].numbered == "letter"
    assert reg["incident"].numbered == "date"
    assert "Plan" not in reg["plan"].field_names  # plan has no fields
    assert [f.name for f in reg["task"].fields] == ["plan"]
