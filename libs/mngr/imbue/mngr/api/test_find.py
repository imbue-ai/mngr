"""Integration tests for the find module (resolve_source_location and ensure_host_started)."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import resolve_source_location
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_ensure_host_started_starts_offline_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that ensure_host_started auto-starts an offline host via the provider."""
    host_id = local_provider.host_id
    offline_host = OfflineHost(
        id=host_id,
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="local",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ),
    )

    online_host, was_started = ensure_host_started(offline_host, is_start_desired=True, provider=local_provider)

    assert was_started
    assert isinstance(online_host, Host)


def test_ensure_host_started_returns_already_online_host(
    local_provider: LocalProviderInstance,
) -> None:
    """Test that ensure_host_started returns an already-online host without starting."""
    host = local_provider.get_host(HostName("localhost"))
    assert isinstance(host, Host)

    online_host, was_started = ensure_host_started(host, is_start_desired=True, provider=local_provider)

    assert not was_started
    assert online_host is host


def test_resolve_source_location_resolves_host_and_path(
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that resolve_source_location returns a valid HostLocation for a known host.

    Verifies the function resolves a host reference and path to an online host
    with a valid HostLocation.
    """
    host_id = local_provider.host_id
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("localhost"),
        provider_name=ProviderInstanceName(LOCAL_PROVIDER_NAME),
    )

    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {host_ref: []}

    result = resolve_source_location(
        source=None,
        source_agent=None,
        source_host=str(host_id),
        source_path=str(temp_work_dir),
        agents_by_host=agents_by_host,
        mngr_ctx=temp_mngr_ctx,
    )

    assert isinstance(result.location.host, OnlineHostInterface)
    assert result.location.path == temp_work_dir
    assert result.agent is None


def test_ensure_host_started_raises_when_start_not_desired(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that ensure_host_started raises UserInputError when offline and start is not desired."""
    host_id = local_provider.host_id
    offline_host = OfflineHost(
        id=host_id,
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="local",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ),
    )

    with pytest.raises(UserInputError):
        ensure_host_started(offline_host, is_start_desired=False, provider=local_provider)
