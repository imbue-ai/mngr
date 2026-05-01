"""Tests for GC behavior with Docker providers, including Docker-offline scenarios.

Verifies that:
- GC completes cleanly when the Docker daemon is unavailable
- _discover_hosts_for_gc includes offline Docker providers with empty host lists
- GC correctly destroys running Docker hosts with no agents
- _discover_hosts_for_gc surfaces both providers (offline Docker + online local)
  so downstream GC resource functions can still process the available provider
"""

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.data_types import GcResourceTypes
from imbue.mngr.api.data_types import GcResult
from imbue.mngr.api.gc import ProviderHosts
from imbue.mngr.api.gc import _discover_hosts_for_gc
from imbue.mngr.api.gc import gc
from imbue.mngr.api.gc import gc_machines
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.testing import make_offline_docker_provider
from imbue.mngr.providers.local.instance import LocalProviderInstance

pytestmark = [pytest.mark.timeout(120)]


# =========================================================================
# Acceptance tests -- fast, no real Docker containers needed
# =========================================================================


@pytest.mark.acceptance
@pytest.mark.docker_sdk
def test_gc_completes_when_docker_daemon_offline(temp_mngr_ctx: MngrContext) -> None:
    """GC should complete without error when the Docker daemon is unreachable.

    Docker's discover_hosts() catches ProviderUnavailableError internally and
    returns an empty list, so gc() processes the provider without errors.
    """
    offline_provider = make_offline_docker_provider(temp_mngr_ctx)

    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[offline_provider],
        resource_types=GcResourceTypes(
            is_machines=True,
            is_snapshots=True,
            is_volumes=True,
            is_work_dirs=True,
            is_logs=True,
            is_build_cache=True,
        ),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert result.errors == []
    assert result.machines_destroyed == []
    assert result.machines_deleted == []


@pytest.mark.acceptance
@pytest.mark.docker_sdk
def test_gc_discover_hosts_returns_empty_hosts_for_offline_provider(temp_mngr_ctx: MngrContext) -> None:
    """_discover_hosts_for_gc includes an offline Docker provider with empty hosts.

    Docker's discover_hosts() catches ProviderUnavailableError internally and
    returns []. The safety for gc_volumes comes from its own catch of
    ProviderUnavailableError when calling list_volumes() -- it skips the
    provider rather than treating all volumes as orphaned.
    """
    offline_provider = make_offline_docker_provider(temp_mngr_ctx)

    result = _discover_hosts_for_gc([offline_provider], temp_mngr_ctx)

    assert len(result) == 1
    provider, hosts = result[0]
    assert provider is offline_provider
    assert hosts == []


@pytest.mark.acceptance
@pytest.mark.docker_sdk
def test_discover_hosts_for_gc_includes_both_providers_when_one_offline(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """_discover_hosts_for_gc should include both providers when one is offline.

    Docker's discover_hosts() catches ProviderUnavailableError internally and
    returns [], so both providers appear in the result. The offline Docker
    provider has empty hosts, and the local provider has its hosts. This lets
    downstream GC resource functions still process the available provider.
    """
    offline_docker = make_offline_docker_provider(temp_mngr_ctx)

    hosts_by_provider = _discover_hosts_for_gc([offline_docker, local_provider], temp_mngr_ctx)

    # Both providers should be present -- Docker with empty hosts, local with its hosts
    provider_names = [p.name for p, _ in hosts_by_provider]
    assert ProviderInstanceName("local") in provider_names
    assert offline_docker.name in provider_names

    # Verify each provider's hosts
    for provider, hosts in hosts_by_provider:
        if provider.name == offline_docker.name:
            assert hosts == []
        elif provider.name == ProviderInstanceName("local"):
            # Local provider should have at least one host (localhost)
            assert len(hosts) >= 1
        else:
            raise AssertionError(f"Unexpected provider in results: {provider.name}")


# =========================================================================
# Release tests -- slower, require real Docker
# =========================================================================


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.docker_sdk
def test_gc_machines_destroys_running_docker_host_with_no_agents(
    docker_provider: DockerProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """GC should destroy a running Docker host that has no agents.

    Overrides the 10-minute min-age GC guard (ae44584ac) via the
    ``config.providers[<name>]`` override hook. The guard protects real
    hosts from transient empty-agent windows; this test wants GC to
    destroy a freshly created host without waiting 10 minutes. Done via
    proper model_copy_update (no monkeypatch, no subclass swap).
    """
    zero_age_provider_config = ProviderInstanceConfig(
        backend=ProviderBackendName("docker"),
        min_online_host_age_seconds=0.0,
    )
    new_providers = {**temp_mngr_ctx.config.providers, docker_provider.name: zero_age_provider_config}
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, new_providers),
    )
    temp_mngr_ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, new_config),
    )
    # Rebind the provider to the new context so get_min_online_host_age_seconds
    # reads the zero-age override.
    docker_provider = docker_provider.model_copy_update(
        to_update(docker_provider.field_ref().mngr_ctx, temp_mngr_ctx),
    )

    host = docker_provider.create_host(HostName("test-gc-destroy"))
    host_id = host.id

    hosts_by_provider: ProviderHosts = [
        (docker_provider, docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group, include_destroyed=True))
    ]

    result = GcResult()
    gc_machines(
        mngr_ctx=temp_mngr_ctx,
        hosts_by_provider=hosts_by_provider,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert len(result.machines_destroyed) == 1
    assert result.machines_destroyed[0].host_id == host_id

    with pytest.raises(HostNotFoundError):
        docker_provider.get_host(host_id)
