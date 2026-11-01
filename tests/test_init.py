"""v3 init: scaffold, lock, seed, adoption, idempotence."""
import pytest
import yaml

from proj import scaffold
from proj.core import db as dbmod
from proj.core.templates import bundled_dir, load_registry
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
    assert "> **status:** `✅ Accepted`" in text
    assert "# DEC-0001. Record architecture decisions" in text  # H1 = "# ID. title"
    # seed cites the ADR pattern (Nygard) and the tool's repo (ADR-tools-style 0001)
    assert "thinkrelevance.com/blog/2011/11/15/documenting-architecture-decisions" in text
    assert "github.com/sihangchen97/proj-tools" in text


def test_lock_written_and_consistent(proj):
    lock = dbmod.read_lock(proj.root)
    assert lock is not None
    assert lock["schema_version"] == 5
    assert "decision" in lock["record_types"]
    # lock spec round-trips to the same schema
    specs = dbmod.specs_from_lock(lock)
    assert specs["decision"].status_allowed == \
        rec.load_registry(proj.root)["decision"].status_allowed


def test_init_idempotent(proj):
    # user-owned files are never recreated or overwritten (templates/ copies)
    (proj.root / "CONTRIBUTING.md").write_text("mine")
    created = scaffold.init(proj, ["default", "quality"])
    assert "CONTRIBUTING.md" not in created
    assert (proj.root / "CONTRIBUTING.md").read_text() == "mine"


def test_init_zoo_rule(proj):
    """--zoo drops the agent rule file; without it, .roo/ is not created."""
    # default init: no rule file, no .roo dir at all
    assert not (proj.root / ".roo").exists()
    rule = proj.root / ".roo" / "rules" / "proj-tools.md"
    assert not rule.exists()
    created = scaffold.init(proj, ["default"], zoo=True)
    assert ".roo/rules/proj-tools.md" in created
    assert "this project is governed by the `proj` tool" in rule.read_text()
    # idempotent: a user-edited rule is never overwritten
    rule.write_text("mine")
    assert ".roo/rules/proj-tools.md" not in scaffold.init(proj, ["default"], zoo=True)
    assert rule.read_text() == "mine"
    # rendered views get re-rendered (they are generated, not user-owned)
    spec = rec.load_registry(proj.root)["decision"]
    db = rec.open_db(proj)
    assert len(db.all(spec)) == 1  # no duplicate seed
    db.close()


def test_task_kind_registered(proj):
    reg = rec.load_registry(proj.root)
    assert reg["task"].numbered == 6
    assert reg["incident"].numbered == 4
    # no per-template contain_task field anymore: task-tree membership comes from
    # the kind=parent relation (any record type can parent tasks), not a template flag
    assert not hasattr(reg["plan"], "contain_task")
    assert not hasattr(reg["task"], "contain_task")


def test_head_blockquote_shape(proj):
    """Record md head: id marker on line 1, head fence (tool-managed region),
    blockquote metadata (no summary row), optional italic summary line below,
    head end fence. No 'summary:' label anywhere."""
    row, p = rec.new_record(proj, "task", "F1", summary="s")
    text = p.read_text()
    lines = text.splitlines()
    assert lines[0] == f"<!-- proj:task {row['id']} -->"
    # the H1 is "# ID. title" (above the head fence; title editable, synced back)
    assert lines[1] == f"# {row['id']}. F1"
    assert lines[3].startswith("<!-- proj:head:start")  # fence below the title
    assert any(line.startswith("> **status:** `✏️ Draft`") for line in lines)
    # created/done merged into one row; not done yet → "… → in progress"
    assert any(line.startswith("> **created/done:**") and "in progress" in line
               for line in lines)
    assert not any(line.startswith("> **done:**") for line in lines)
    # NO summary row in the blockquote (no "summary:" label)
    assert not any("summary" in line for line in lines if line.startswith("> **"))
    # summary is an italic line below the block, only when set
    assert "_s_" in lines
    assert lines.index("_s_") > next(i for i, x in enumerate(lines) if x.startswith("> **"))
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
    _row2, p2 = rec.new_record(proj, "task", "F2")
    text2 = p2.read_text()
    lines2 = text2.splitlines()
    assert "_s_" not in lines2  # no summary line
    assert "<!-- proj:head:end -->" in lines2  # fence end still present
    assert rec.md_summary(text2) == ""


def test_numbered_must_be_positive_int(proj):
    """D9: numbered must be an int > 0 (the id prefix resolves a record's type);
    0 and non-int are rejected when the registry loads."""
    d = proj.root / "profiles" / "default"
    d.mkdir(parents=True)
    base = (bundled_dir() / "default" / "debt.yaml").read_text()
    (d / "debt.yaml").write_text(base.replace("numbered: 4", "numbered: 0"))
    with pytest.raises(ValueError, match="numbered"):
        load_registry(proj.root)
    (d / "debt.yaml").write_text(base.replace("numbered: 4", "numbered: 'letter'"))
    with pytest.raises(ValueError, match="numbered"):
        load_registry(proj.root)


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
        "contain_task": False,
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
