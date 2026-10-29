import pytest

from proj.changelog import add_entry, bump, read, release_notes, version

SEED = """\
# Changelog

## [Unreleased]

## [0.1.0] - 2026-01-01
### Added
- initial release
"""


def test_bump():
    assert bump("0.1.0", "patch") == "0.1.1"
    assert bump("0.1.0", "minor") == "0.2.0"
    assert bump("0.1.0", "major") == "1.0.0"
    with pytest.raises(ValueError):
        bump("abc", "patch")


def test_add_entry_unreleased(cfg):
    cfg.changelog.write_text(SEED)
    add_entry(cfg, "Added", "new feature")
    text = cfg.changelog.read_text()
    assert text.index("[Unreleased]") < text.index("new feature") < text.index("[0.1.0]")


def test_add_entry_creates_section(cfg):
    cfg.changelog.write_text("# Changelog\n")
    add_entry(cfg, "Fixed", "a bug", version_name="0.1.0")
    text = cfg.changelog.read_text()
    assert "## [0.1.0]" in text
    assert "### Fixed" in text
    assert "- a bug" in text


def test_version_creates_release(cfg):
    cfg.changelog.write_text(SEED)
    v = version(cfg, part="minor")
    assert v == "0.2.0"
    text = cfg.changelog.read_text()
    assert "## [0.2.0]" in text
    # fresh Unreleased must now be first
    names = [x.name for x in read(cfg)[1]]
    assert names[0] == "Unreleased"
    with pytest.raises(ValueError):
        version(cfg, explicit="0.2.0")


def test_release_notes(cfg):
    cfg.changelog.write_text(SEED)
    notes = release_notes(cfg, "0.1.0")
    assert "# 0.1.0" in notes
    assert "initial release" in notes
    with pytest.raises(ValueError):
        release_notes(cfg, "9.9.9")
