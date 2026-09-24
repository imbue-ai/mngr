import shutil
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.providers import _instance_cache
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr.providers.mock_provider_test import DiscoveryRecordingProvider
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.providers.mock_provider_test import make_offline_host


@pytest.fixture
def gc_mock_provider(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> MockProviderInstance:
    """Create a MockProviderInstance for gc_machines tests."""
    return MockProviderInstance(
        name=ProviderInstanceName("test-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )


@pytest.fixture
def noop_binary() -> str:
    """A cross-platform path to a no-op binary that accepts any arguments.

    Use this as a fake mngr_binary for AgentObserver tests. On macOS /bin/true
    does not exist (it lives at /usr/bin/true), so shutil.which() finds the
    correct path on any platform.
    """
    path = shutil.which("true")
    assert path is not None, "Could not find 'true' binary on this system"
    return path


class PinnedLookupHarness(FrozenModel):
    """Two configured providers owning stopped hosts, wired into a context that discovery can load.

    The first provider also owns a sibling host, so a lookup that reads more than the pinned host shows.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mngr_ctx: MngrContext = Field(description="Context whose configuration names both providers")
    first_provider: DiscoveryRecordingProvider = Field(description="Provider owning the first host")
    second_provider: DiscoveryRecordingProvider = Field(description="Provider owning the second host")
    first_host_id: HostId = Field(description="The first provider's host that tests pin")
    first_sibling_host_id: HostId = Field(description="The first provider's other host, which no test pins")
    second_host_id: HostId = Field(description="The second provider's only host")


def _add_stopped_host(provider: DiscoveryRecordingProvider, host_name: str, stop_reason: HostState) -> HostId:
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name=host_name,
        created_at=now,
        updated_at=now,
        stop_reason=stop_reason.value,
    )
    provider.mock_hosts.append(make_offline_host(certified_data, provider, provider.mngr_ctx))
    return host_id


class PinnedLookupHarnessFactory(FrozenModel):
    """Builds :class:`PinnedLookupHarness` instances over one test's host dir and context."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    host_dir: Path = Field(description="Host dir the providers' hosts live under")
    base_mngr_ctx: MngrContext = Field(description="Context the harness context is derived from")

    def build(
        self,
        *,
        is_second_provider_enabled: bool = True,
        is_full_discovery: bool = False,
        first_host_stop_reason: HostState = HostState.STOPPED,
    ) -> PinnedLookupHarness:
        """Build a harness whose providers are registered as the context's built instances.

        Both discovery and pinned lookups then resolve the providers by name. ``is_full_discovery`` is the
        ``--safe`` mode.
        """
        first_name = ProviderInstanceName("pinned-first")
        second_name = ProviderInstanceName("pinned-second")
        config = self.base_mngr_ctx.config.model_copy_update(
            to_update(
                self.base_mngr_ctx.config.field_ref().providers,
                {
                    first_name: LocalProviderConfig(),
                    second_name: LocalProviderConfig(is_enabled=is_second_provider_enabled),
                },
            ),
        )
        mngr_ctx = self.base_mngr_ctx.model_copy_update(
            to_update(self.base_mngr_ctx.field_ref().config, config),
            to_update(self.base_mngr_ctx.field_ref().is_full_discovery, is_full_discovery),
        )
        first_provider = DiscoveryRecordingProvider(name=first_name, host_dir=self.host_dir, mngr_ctx=mngr_ctx)
        second_provider = DiscoveryRecordingProvider(name=second_name, host_dir=self.host_dir, mngr_ctx=mngr_ctx)
        first_host_id = _add_stopped_host(first_provider, "pinned-first-host", first_host_stop_reason)
        first_sibling_host_id = _add_stopped_host(first_provider, "pinned-first-sibling-host", HostState.STOPPED)
        second_host_id = _add_stopped_host(second_provider, "pinned-second-host", HostState.STOPPED)
        _instance_cache[(first_name, id(mngr_ctx))] = first_provider
        _instance_cache[(second_name, id(mngr_ctx))] = second_provider
        return PinnedLookupHarness(
            mngr_ctx=mngr_ctx,
            first_provider=first_provider,
            second_provider=second_provider,
            first_host_id=first_host_id,
            first_sibling_host_id=first_sibling_host_id,
            second_host_id=second_host_id,
        )


@pytest.fixture
def make_pinned_lookup_harness(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> PinnedLookupHarnessFactory:
    """A :class:`PinnedLookupHarnessFactory`; the ``temp_mngr_ctx`` fixture clears the instance cache on teardown."""
    return PinnedLookupHarnessFactory(host_dir=temp_host_dir, base_mngr_ctx=temp_mngr_ctx)


@pytest.fixture
def pinned_lookup_harness(make_pinned_lookup_harness: PinnedLookupHarnessFactory) -> PinnedLookupHarness:
    """A :class:`PinnedLookupHarness` with both providers enabled, ordinary discovery, and stopped hosts."""
    return make_pinned_lookup_harness.build()
