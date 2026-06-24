"""Unit coverage for ``_is_discovery_complete``.

The Electron shell holds its loading screen -- and gates dead-workspace
filtering on window restore -- until the backend reports discovery has finished
its first full sweep. ``_is_discovery_complete`` is that signal: a *fresh* full
discovery snapshot. A cached last-good snapshot replayed from disk carries its
original (old) timestamp, so it must NOT read as complete; only a recent poll
counts.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.minds.desktop_client.app import _DISCOVERY_FRESHNESS_THRESHOLD_SECONDS
from imbue.minds.desktop_client.app import _is_discovery_complete
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver


def test_incomplete_before_any_snapshot() -> None:
    """A resolver that has never received a full snapshot is not complete."""
    resolver = MngrCliBackendResolver()
    assert _is_discovery_complete(resolver) is False


def test_complete_after_fresh_snapshot() -> None:
    """A full snapshot observed just now marks discovery complete."""
    resolver = MngrCliBackendResolver()
    resolver.update_providers(
        providers=(),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    assert _is_discovery_complete(resolver) is True


def test_incomplete_for_stale_cached_snapshot() -> None:
    """A snapshot older than the freshness window (e.g. a cached last-good
    snapshot from a prior run) does not count as complete."""
    resolver = MngrCliBackendResolver()
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=_DISCOVERY_FRESHNESS_THRESHOLD_SECONDS + 60)
    resolver.update_providers(
        providers=(),
        error_by_provider_name={},
        last_full_snapshot_at=stale_at,
    )
    assert _is_discovery_complete(resolver) is False


def test_non_mngr_resolver_is_always_complete() -> None:
    """Resolvers without an async discovery pipeline have synchronous state, so
    they are treated as already complete (never gate the loading screen)."""
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    assert _is_discovery_complete(resolver) is True
