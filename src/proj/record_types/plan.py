"""Plan record_type: no class of its own — the whole behavior is template
data in profiles/default/plan.yaml (state-machine verbs, a `type: task`
Task List section). Module-level delegations keep the cli/tests call shape.
"""
from __future__ import annotations

from proj.core.config import ProjConfig
from proj.engine import store as rec
from proj.engine.record import record_for


def _obj(cfg: ProjConfig):
    return record_for("plan", rec.load_registry(cfg.root))


def new(cfg: ProjConfig, title: str, summary: str = ""):
    return _obj(cfg).new(cfg, title, summary=summary)


def transition(cfg: ProjConfig, verb: str, id_: str):
    return _obj(cfg).transition(cfg, verb, id_)


def list_plans(cfg: ProjConfig, status: str | None = None) -> list[dict]:
    return _obj(cfg).list(cfg, status)
