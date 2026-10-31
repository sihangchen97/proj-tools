"""Record subclasses. Importing this package registers all Record subclasses
(`@register` runs on module import); `record_for` resolves lazily."""
import proj.record_types.changelog
import proj.record_types.decision
import proj.record_types.plan
import proj.record_types.task  # noqa: F401 — import for its @register side effect
