"""D6: the project .env supplies config defaults (OS env wins, no override)."""
import os

from proj.core.config import load_project_env


def test_load_project_env_semantics(proj, monkeypatch):
    (proj.root / ".env").write_text(
        "# comment line\n\nPROJ_LOGS=2\nPROJ_TEST_A=from_env\n"
        "PROJ_TEST_B = spaced\n", encoding="utf-8")
    monkeypatch.delenv("PROJ_LOGS", raising=False)
    monkeypatch.delenv("PROJ_TEST_B", raising=False)
    monkeypatch.setenv("PROJ_TEST_A", "from_os")  # existing OS var: never overridden
    load_project_env(proj.root)
    assert os.environ["PROJ_LOGS"] == "2"          # filled from .env
    assert os.environ["PROJ_TEST_A"] == "from_os"  # OS env wins
    assert os.environ["PROJ_TEST_B"] == "spaced"   # value stripped
    # idempotent: a second load is a no-op
    load_project_env(proj.root)
    assert os.environ["PROJ_LOGS"] == "2"


def test_load_project_env_missing_file(proj, monkeypatch):
    monkeypatch.delenv("PROJ_LOGS", raising=False)
    load_project_env(proj.root)  # no .env → no-op, no error
    assert "PROJ_LOGS" not in os.environ
