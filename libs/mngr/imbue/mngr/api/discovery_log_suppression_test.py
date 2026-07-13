from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.mngr.api.discovery_events import DISCOVERY_EVENT_SOURCE
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import make_provider_discovery_snapshot_event
from imbue.mngr.api.discovery_log_suppression import DiscoveryErrorLogSuppressor
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import capture_loguru

_BASE_TIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _error_event(
    source_name: str,
    error_type: str = "ProviderNotAuthorizedError",
    error_message: str = "credentials not configured",
    provider_name: str | None = None,
) -> DiscoveryErrorEvent:
    return DiscoveryErrorEvent(
        timestamp=IsoTimestamp(format_nanosecond_iso_timestamp(_BASE_TIME)),
        event_id=EventId(generate_log_event_id()),
        source=DISCOVERY_EVENT_SOURCE,
        error_type=error_type,
        error_message=error_message,
        source_name=source_name,
        provider_name=provider_name,
    )


def _snapshot(provider: str, error: DiscoveryError | None = None) -> ProviderDiscoverySnapshotEvent:
    return make_provider_discovery_snapshot_event(
        provider_name=ProviderInstanceName(provider),
        agents=(),
        hosts=(),
        discovery_started_at=_BASE_TIME,
        discovery_finished_at=_BASE_TIME + timedelta(seconds=1),
        error=error,
    )


def test_provider_level_error_logs_once_and_suppresses_repeats() -> None:
    suppressor = DiscoveryErrorLogSuppressor()
    event = _error_event(source_name="vultr", provider_name="vultr")
    with capture_loguru(level="WARNING") as log_output:
        suppressor.log_discovery_error_event(event)
        suppressor.log_discovery_error_event(event)
        suppressor.log_discovery_error_event(event)
    lines = [line for line in log_output.getvalue().splitlines() if line]
    assert len(lines) == 1
    assert "Discovery error from vultr" in lines[0]
    assert "suppressing repeats" in lines[0]


def test_changed_error_logs_again() -> None:
    suppressor = DiscoveryErrorLogSuppressor()
    first = _error_event(source_name="gcp", provider_name="gcp", error_message="no project configured")
    second = _error_event(source_name="gcp", provider_name="gcp", error_message="token expired")
    with capture_loguru(level="WARNING") as log_output:
        suppressor.log_discovery_error_event(first)
        suppressor.log_discovery_error_event(second)
        suppressor.log_discovery_error_event(second)
    lines = [line for line in log_output.getvalue().splitlines() if line]
    assert len(lines) == 2
    assert "no project configured" in lines[0]
    assert "token expired" in lines[1]


def test_clean_snapshot_logs_recovery_and_rearms() -> None:
    suppressor = DiscoveryErrorLogSuppressor()
    event = _error_event(source_name="ovh", provider_name="ovh")
    with capture_loguru(level="INFO") as log_output:
        suppressor.log_discovery_error_event(event)
        suppressor.record_provider_snapshot(_snapshot("ovh"))
        suppressor.log_discovery_error_event(event)
    lines = [line for line in log_output.getvalue().splitlines() if line]
    assert len(lines) == 3
    assert "recovered" in lines[1]
    assert "Discovery error from ovh" in lines[2]


def test_recovery_is_only_logged_for_providers_whose_error_was_logged() -> None:
    suppressor = DiscoveryErrorLogSuppressor()
    with capture_loguru(level="INFO") as log_output:
        suppressor.record_provider_snapshot(_snapshot("docker"))
    assert log_output.getvalue() == ""


def test_errored_snapshot_does_not_rearm_suppression() -> None:
    suppressor = DiscoveryErrorLogSuppressor()
    event = _error_event(source_name="azure", provider_name="azure")
    errored_snapshot = _snapshot(
        "azure",
        error=DiscoveryError(
            type_name="ProviderNotAuthorizedError",
            message="credentials not configured",
            provider_name=ProviderInstanceName("azure"),
        ),
    )
    with capture_loguru(level="INFO") as log_output:
        suppressor.log_discovery_error_event(event)
        suppressor.record_provider_snapshot(errored_snapshot)
        suppressor.log_discovery_error_event(event)
    lines = [line for line in log_output.getvalue().splitlines() if line]
    assert len(lines) == 1


def test_host_attributed_errors_always_log() -> None:
    suppressor = DiscoveryErrorLogSuppressor()
    # A host-level failure carries the owning provider's name but its source is the host id.
    event = _error_event(
        source_name="host-1234",
        error_type="KeyError",
        error_message="'ssh'",
        provider_name="local",
    )
    with capture_loguru(level="WARNING") as log_output:
        suppressor.log_discovery_error_event(event)
        suppressor.log_discovery_error_event(event)
    lines = [line for line in log_output.getvalue().splitlines() if line]
    assert len(lines) == 2
    assert all("suppressing repeats" not in line for line in lines)


def test_error_without_provider_name_always_logs() -> None:
    suppressor = DiscoveryErrorLogSuppressor()
    event = _error_event(source_name="something", error_type="RuntimeError", error_message="boom")
    with capture_loguru(level="WARNING") as log_output:
        suppressor.log_discovery_error_event(event)
        suppressor.log_discovery_error_event(event)
    lines = [line for line in log_output.getvalue().splitlines() if line]
    assert len(lines) == 2


def test_suppression_is_per_provider() -> None:
    suppressor = DiscoveryErrorLogSuppressor()
    vultr_event = _error_event(source_name="vultr", provider_name="vultr")
    ovh_event = _error_event(source_name="ovh", provider_name="ovh")
    with capture_loguru(level="WARNING") as log_output:
        suppressor.log_discovery_error_event(vultr_event)
        suppressor.log_discovery_error_event(ovh_event)
        suppressor.log_discovery_error_event(vultr_event)
        suppressor.log_discovery_error_event(ovh_event)
    lines = [line for line in log_output.getvalue().splitlines() if line]
    assert len(lines) == 2
