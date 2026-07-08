"""Tests for GC behavior with Docker providers, including Docker-offline scenarios.

Verifies that:
- GC completes cleanly when the Docker daemon is unavailable
- _discover_hosts_for_gc skips an offline Docker provider entirely (its
  discover_hosts raises ProviderUnavailableError rather than returning []),
  which is what prevents gc_volumes from treating its volumes as orphaned
- GC correctly destroys running Docker hosts with no agents
- _discover_hosts_for_gc still surfaces the available provider (online local)
  when another provider (offline Docker) is skipped
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
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
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
@pytest.mark.allow_warnings(match=r"Failed to discover hosts for provider")
def test_gc_completes_when_docker_daemon_offline(temp_mngr_ctx: MngrContext) -> None:
    """GC should complete without error when the Docker daemon is unreachable.

    Docker's discover_hosts() raises ProviderUnavailableError when the daemon is
    unreachable, so _discover_hosts_for_gc skips the provider entirely and gc()
    completes with nothing to do (and no errors).
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
@pytest.mark.allow_warnings(match=r"Failed to discover hosts for provider")
def test_gc_discover_hosts_skips_offline_provider(temp_mngr_ctx: MngrContext) -> None:
    """_discover_hosts_for_gc skips a Docker provider whose daemon is unreachable.

    Docker's discover_hosts() raises ProviderUnavailableError when the daemon is
    unreachable (rather than returning []), so _discover_hosts_for_gc drops the
    provider entirely. This is what prevents gc_volumes from seeing an empty host
    list and deleting every volume as "orphaned".
    """
    offline_provider = make_offline_docker_provider(temp_mngr_ctx)

    result = _discover_hosts_for_gc([offline_provider], temp_mngr_ctx)

    assert result == []


@pytest.mark.acceptance
@pytest.mark.docker_sdk
@pytest.mark.allow_warnings(match=r"Failed to discover hosts for provider")
def test_discover_hosts_for_gc_skips_offline_docker_keeps_online(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """_discover_hosts_for_gc drops an offline Docker provider but keeps available ones.

    Docker's discover_hosts() raises ProviderUnavailableError when the daemon is
    unreachable, so _discover_hosts_for_gc skips it while still processing the
    online local provider. Skipping the offline provider -- rather than including
    it with an empty host list -- is what keeps gc_volumes from treating its
    volumes as orphaned.
    """
    offline_docker = make_offline_docker_provider(temp_mngr_ctx)

    hosts_by_provider = _discover_hosts_for_gc([offline_docker, local_provider], temp_mngr_ctx)

    # Only the available local provider should remain; the offline Docker provider
    # is skipped entirely.
    provider_names = [p.name for p, _ in hosts_by_provider]
    assert ProviderInstanceName("local") in provider_names
    assert offline_docker.name not in provider_names

    for provider, hosts in hosts_by_provider:
        assert provider.name == ProviderInstanceName("local")
        # Local provider should have at least one host (localhost).
        assert len(hosts) >= 1


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

    destroy_host marks the host as DESTROYED but preserves the record so
    gc_snapshots can age-gate snapshot cleanup. The record itself is
    purged by a later gc_machines pass (via delete_host) once the host
    has aged past destroyed_host_persisted_seconds.

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

    # Record persists in DESTROYED state; default discovery excludes it,
    # but include_destroyed=True surfaces it for gc_snapshots.
    assert docker_provider.get_host(host_id).get_state() == HostState.DESTROYED
    default_hosts = docker_provider.discover_hosts(temp_mngr_ctx.concurrency_group)
    assert host_id not in {h.host_id for h in default_hosts}
