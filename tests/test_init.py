"""v3 init: scaffold, lock, seed, adoption, idempotence."""
from proj import scaffold
from proj.core import db as dbmod
from proj.engine import store as rec


def test_init_creates_expected_files(proj):
    root = proj.root
    for expected in ["CHANGELOG.md", "TASKLIST.md", "CONTRIBUTING.md",
                     ".proj.lock",
                     "docs/decisions/0001-record-architecture-decisions.md",
                     ".env.example", ".github/PULL_REQUEST_TEMPLATE.md"]:
        assert (root / expected).is_file(), expected
    # DB is .proj.sqlite at the project root (committed by default);
    # record_type profiles are NOT copied (bundled fallback), but the global
    # changelog config IS (every project keeps its own editable copy)
    assert proj.db_path.is_file()
    assert proj.db_path == root / ".proj.sqlite"
    assert (root / "profiles" / "changelog.yaml").is_file()
    assert not (root / "profiles" / "default").exists()


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
    assert "status: ✅ Accepted" in text
    assert "# 1. Record architecture decisions" in text


def test_lock_written_and_consistent(proj):
    lock = dbmod.read_lock(proj.root)
    assert lock is not None
    assert lock["schema_version"] == 1
    assert "decision" in lock["record_types"]
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
    assert reg["incident"].numbered == 4
    assert "Plan" not in reg["plan"].field_names  # plan has no fields
    assert [f.name for f in reg["task"].fields] == ["parent", "related"]


def test_front_matter_shape(proj):
    """Record md head: a VALID-YAML front matter (gray box) on line 1 ('---',
    status with emoji, derived rows always shown), the uuid marker comment
    just below the closing '---' (invisible in preview)."""
    row, p = rec.new_record(proj, "task", "F1", summary="s")
    text = p.read_text()
    lines = text.splitlines()
    assert lines[0] == "---"
    assert lines[1] == "status: 🕐 Not Started"
    assert f"<!-- proj:task {row['uuid']} -->" in lines
    assert lines.index("---") < lines.index(f"<!-- proj:task {row['uuid']} -->")
    assert "started: —" in lines and "done: —" in lines
    # summary lives BELOW the box (heading → summary → changelog), not in it
    assert "Summary: s" in lines
    assert lines.index("---") < lines.index("Summary: s")
    # round-trip: the marker and managed values survive parse
    (mk, u), _t, _s = rec.parse_record(text)
    assert (mk, u) == ("task", row["uuid"])
    assert rec.md_status(text) == "Not Started"
    assert rec.md_summary(text) == "s"
    # legacy shape (marker line 1 + `Status:` line) still parses
    legacy = (f"<!-- proj:task {row['uuid']} -->\n\n# F1\n\nStatus: Done\n\n## Steps\n")
    (mk, u), _t, _s = rec.parse_record(legacy)
    assert (mk, u) == ("task", row["uuid"])
    assert rec.md_status(legacy) == "Done"
    # old shape (marker INSIDE the front-matter block) still parses, too
    old = (f"---\n<!-- proj:task {row['uuid']} -->\nstatus: Done\n---\n\n# F1\n\n## Steps\n")
    (mk, u), _t, _s = rec.parse_record(old)
    assert (mk, u) == ("task", row["uuid"])
    assert rec.md_status(old) == "Done"
