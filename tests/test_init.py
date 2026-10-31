"""v3 init: scaffold, lock, seed, adoption, idempotence."""
from proj import scaffold
from proj.core import db as dbmod
from proj.engine import store as rec


def test_init_creates_expected_files(proj):
    root = proj.root
    for expected in ["CHANGELOG.md", "TASKLIST.md", "CONTRIBUTING.md",
                     ".proj.lock",
                     "docs/decisions/0001-record-architecture-decisions.md",
                     ".env.example"]:
        assert (root / expected).is_file(), expected
    # .github (issue/PR templates) is not scaffolded — project-specific
    assert not (root / ".github").exists()
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
    assert "> **status:** ✅ Accepted" in text
    assert "# 1. Record architecture decisions" in text
    # seed cites the ADR pattern (Nygard) and the tool's repo (ADR-tools-style 0001)
    assert "thinkrelevance.com/blog/2011/11/15/documenting-architecture-decisions" in text
    assert "github.com/sihangchen97/proj-tools" in text


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


def test_head_blockquote_shape(proj):
    """Record md head: the uuid marker comment on line 1 (invisible in preview),
    then a blockquote of `> **key:** value` lines (gray sidebar, NOT front matter,
    each line hard-wrapped with two trailing spaces, derived rows always shown)."""
    row, p = rec.new_record(proj, "task", "F1", summary="s")
    text = p.read_text()
    lines = text.splitlines()
    assert lines[0] == f"<!-- proj:task {row['uuid']} -->"
    assert lines[1] == "# A. F1"  # title directly below the marker (tasks use letters)
    assert "> **status:** 🕐 Not Started  " in lines  # blockquote below the title
    assert "> **started:** —  " in lines and "> **done:** —  " in lines
    # summary lives BELOW the head (heading → summary → changelog)
    assert "Summary: s" in lines
    assert lines.index(f"<!-- proj:task {row['uuid']} -->") < lines.index("Summary: s")
    # no front matter anywhere: the head is a blockquote, not a --- block
    assert not text.startswith("---")
    # round-trip: the marker and managed values survive parse
    (mk, u), _t, _s = rec.parse_record(text)
    assert (mk, u) == ("task", row["uuid"])
    assert rec.md_status(text) == "Not Started"
    assert rec.md_summary(text) == "s"
