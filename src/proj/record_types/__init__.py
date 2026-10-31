"""Kind subclasses. Importing this package registers all Record subclasses."""
from proj.record_types.changelog import *  # noqa: F403 — keeps changelog importable as before
from proj.record_types.decision import Decision  # noqa: F401
from proj.record_types.plan import *  # noqa: F403 — plan has no class; import for module-level delegations
from proj.record_types.task import Task  # noqa: F401
