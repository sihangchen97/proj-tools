from proj.scaffold import init


def test_init_core_creates_expected_files(cfg):
    init(cfg, ["core"])
    for expected in ["CHANGELOG.md", "TASKLIST.md", "CONTRIBUTING.md",
                     "docs/decisions/0001-record-architecture-decisions.md",
                     "templates/default/decision.md",
                     ".github/ISSUE_TEMPLATE/bug.md",
                     ".github/PULL_REQUEST_TEMPLATE.md"]:
        assert (cfg.root / expected).is_file(), expected
    assert cfg.adr_dir.is_dir()
    assert cfg.plans_dir.is_dir()
    assert cfg.proj_dir.is_dir()


def test_init_idempotent(cfg):
    init(cfg, ["core"])
    (cfg.root / "TASKLIST.md").write_text("mine")
    created = init(cfg, ["core"])
    assert "TASKLIST.md" not in created
    assert (cfg.root / "TASKLIST.md").read_text() == "mine"
    # decision 0001 must not be recreated
    assert len(list(cfg.adr_dir.glob("0001-*.md"))) == 1


def test_init_tiers(cfg):
    init(cfg, ["cv", "quality"])
    assert (cfg.root / "DATASET.md").is_file()
    assert (cfg.root / "BENCHMARK.md").is_file()
    assert (cfg.root / ".env.example").is_file()
    assert cfg.debt_dir.is_dir()
    assert cfg.incidents_dir.is_dir()
    assert cfg.experiments_dir.is_dir()
