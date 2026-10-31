"""v3 init: scaffold, lock, seed, adoption, idempotence."""
import yaml

from proj import scaffold
from proj.core import db as dbmod
from proj.core.templates import load_registry
from proj.engine import store as rec


def test_init_creates_expected_files(proj):
    root = proj.root
    for expected in ["CHANGELOG.md", "TASKLIST.md", "CONTRIBUTING.md",
                     ".proj.lock",
                     "docs/decisions/DEC-0001-record-architecture-decisions.md",
                     ".env.example"]:
        assert (root / expected).is_file(), expected
    # .github (issue/PR templates) is not scaffolded — project-specific
    assert not (root / ".github").exists()
    # DB is .proj.sqlite at the project root (committed by default);
    # nothing from profiles/ is copied (bundled fallback — record_type
    # templates and the global changelog config alike)
    assert proj.db_path.is_file()
    assert proj.db_path == root / ".proj.sqlite"
    assert not (root / "profiles").exists()


def test_seed_decision_content(proj):
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    rows = db.all(spec)
    db.close()
    assert len(rows) == 1
    assert rows[0]["id"] == "DEC-0001"
    assert rows[0]["status"] == "Accepted"
    text = (proj.root / "docs/decisions/DEC-0001-record-architecture-decisions.md").read_text()
    assert "<!-- proj:decision " in text
    assert "> **status:** ✅ Accepted" in text
    assert "# DEC-0001. Record architecture decisions" in text
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
    created = scaffold.init(proj, ["default", "quality"])
    assert "CONTRIBUTING.md" not in created
    assert (proj.root / "CONTRIBUTING.md").read_text() == "mine"
    # rendered views get re-rendered (they are generated, not user-owned)
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    assert len(db.all(spec)) == 1  # no duplicate seed
    db.close()


def test_task_kind_registered(proj):
    reg = rec.load_registry(proj.root)
    assert reg["task"].numbered == 6
    assert reg["incident"].numbered == 4
    assert "Plan" not in reg["plan"].field_names  # plan has no fields
    assert {f.name for f in reg["task"].fields} == {"parent"}  # related is a relation, not a field


def test_head_blockquote_shape(proj):
    """Record md head: id marker on line 1, head fence (tool-managed region),
    blockquote metadata (no summary row), optional italic summary line below,
    head end fence. No 'summary:' label anywhere."""
    row, p = rec.new_record(proj, "task", "F1", summary="s")
    text = p.read_text()
    lines = text.splitlines()
    assert lines[0] == f"<!-- proj:task {row['id']} -->"
    # head fence start (tool-managed instruction)
    assert lines[1].startswith("<!-- proj:head:start")
    assert lines[2] == "# T-000001. F1"  # title directly below the fence
    assert "> **status:** ✏️ Draft  " in lines  # blockquote below the title
    assert any(line.startswith("> **created:**") for line in lines)  # fixed three rows
    assert "> **done:** —" in lines
    # NO summary row in the blockquote (no "summary:" label)
    assert not any("summary" in line for line in lines if line.startswith("> **"))
    # summary is an italic line below the block, only when set
    assert "_s_" in lines
    assert lines.index("_s_") > lines.index("> **done:** —")
    # head fence end
    assert lines.index("<!-- proj:head:end -->") > lines.index("_s_")
    # no front matter anywhere
    assert not text.startswith("---")
    # round-trip: marker + summary survive parse
    (mk, u), _t, _s = rec.parse_record(text)
    assert (mk, u) == ("task", row["id"])
    assert rec.md_status(text) == "Draft"
    assert rec.md_summary(text) == "s"
    # a record with no summary: no italic line, head end fence still present
    row2, p2 = rec.new_record(proj, "task", "F2")
    text2 = p2.read_text()
    lines2 = text2.splitlines()
    assert "_s_" not in lines2  # no summary line
    assert "<!-- proj:head:end -->" in lines2  # fence end still present
    assert rec.md_summary(text2) == ""


def test_profile_dir_inert_until_selected(proj):
    """Unselected profiles/<name>/ subdirs are not scanned; init --profile
    selects them (lock-pinned, default first) and their record_types register."""
    reg = load_registry(proj.root)
    assert "research" not in reg
    # drop a research profile (valid shape: 3-letter code + 4 digits = 7)
    d = proj.root / "profiles" / "research"
    d.mkdir(parents=True)
    (d / "research.yaml").write_text(yaml.safe_dump({
        "record_type": "research", "code": "RES", "dir": "docs/research",
        "numbered": 4,
        "features": {"supersede": False, "task": False},
        "render": {"changelog": True},
        "verbs": {"close": {"from": "Any", "to": "Closed"}},
        "status-allowed": [
            {"name": "Draft", "emoji": "\u270f\ufe0f"},
            {"name": "Closed", "emoji": "\u2705", "done": True}],
        "sections": [{"name": "Question"}],
    }))
    # still inert: default only
    assert "research" not in load_registry(proj.root)
    assert "research" not in reg  # no lock churn yet

    # select it on a repeat init
    scaffold.init(proj, ["research"])
    reg = load_registry(proj.root)
    assert "research" in reg
    assert (proj.root / "docs/research").is_dir()  # dir scaffolded from spec
    lock = dbmod.read_lock(proj.root)
    # expanded: fixture locked default+quality, research appended (default first)
    assert lock["profiles"] == ["default", "quality", "research"]

    # idempotent: no duplicate append, lock untouched
    scaffold.init(proj, ["research"])
    lock = dbmod.read_lock(proj.root)
    assert lock["profiles"] == ["default", "quality", "research"]
