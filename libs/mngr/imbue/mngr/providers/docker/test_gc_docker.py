"""Tests for GC behavior with Docker providers, including Docker-offline scenarios.

Verifies that:
- GC completes cleanly when the Docker daemon is unavailable
- _discover_hosts_for_gc includes offline Docker providers with empty host lists
- GC correctly destroys running Docker hosts with no agents
- _discover_hosts_for_gc surfaces both providers (offline Docker + online local)
  so downstream GC resource functions can still process the available provider
- gc_orphaned_resources reaps Docker containers and ``mngr-build-host-*`` images
  that no longer correspond to a known host.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.data_types import GcResourceTypes
from imbue.mngr.api.data_types import GcResult
from imbue.mngr.api.gc import ProviderHosts
from imbue.mngr.api.gc import _discover_hosts_for_gc
from imbue.mngr.api.gc import gc
from imbue.mngr.api.gc import gc_machines
from imbue.mngr.api.gc import gc_orphaned_resources
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.instance import LABEL_HOST_ID
from imbue.mngr.providers.docker.instance import LABEL_PROVIDER
from imbue.mngr.providers.docker.instance import parse_build_image_host_id
from imbue.mngr.providers.docker.testing import make_docker_provider
from imbue.mngr.providers.docker.testing import make_offline_docker_provider
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.local.volume import LocalVolume

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
            is_orphaned_resources=True,
        ),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert result.errors == []
    assert result.machines_destroyed == []
    assert result.machines_deleted == []
    assert result.containers_destroyed == []
    assert result.images_destroyed == []


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


# =========================================================================
# Orphan reconciliation -- Docker containers and build images
# =========================================================================


def _old_iso_timestamp() -> str:
    """An RFC3339 timestamp comfortably past any reasonable grace window."""
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")


class _FakeContainer:
    """In-memory stand-in for ``docker.models.containers.Container``.

    Enough surface for ``gc_orphaned_resources`` to run: ``id``, ``short_id``,
    ``name``, ``labels``, ``status``, ``attrs["Created"]``, and a ``remove``
    that flips a flag instead of touching Docker. Tests assert on ``removed``
    directly.

    ``status`` defaults to ``"exited"`` because orphan reconciliation primarily
    targets stopped containers; tests that simulate a live container can pass
    ``status="running"`` explicitly.
    """

    def __init__(
        self,
        *,
        container_id: str,
        name: str,
        labels: dict[str, str],
        created: str,
        status: str = "exited",
    ) -> None:
        self.id = container_id
        self.short_id = container_id[:12]
        self.name = name
        self.labels = labels
        self.status = status
        self.attrs = {"Created": created}
        self.removed = False

    def reload(self) -> None:
        # SDK refresh hook -- our state never changes between calls.
        return None

    def remove(self, force: bool = False) -> None:
        assert force, "orphan reconciliation must force-remove containers"
        self.removed = True


class _FakeImage:
    """In-memory stand-in for ``docker.models.images.Image``."""

    def __init__(
        self,
        *,
        image_id: str,
        tags: list[str],
        created: str,
        size: int,
    ) -> None:
        self.id = image_id
        self.short_id = image_id[:12]
        self.tags = list(tags)
        self.attrs = {"Created": created, "Size": size}


class _FakeContainerCollection:
    def __init__(self, items: list[_FakeContainer]) -> None:
        self._items = items

    def list(self, all: bool = False, filters: object = None) -> list[_FakeContainer]:
        # The orphan path always passes all=True; we don't honor label filters
        # here because the production code does its own name-prefix filtering.
        del all, filters
        return [c for c in self._items if not c.removed]


class _FakeImageCollection:
    def __init__(self, items: list[_FakeImage]) -> None:
        self._items = items
        self.removed_ids: list[str] = []

    def list(self) -> list[_FakeImage]:
        return list(self._items)

    def remove(self, image_id: str, force: bool = False) -> None:
        assert force, "orphan reconciliation must force-remove images"
        self.removed_ids.append(image_id)
        self._items = [img for img in self._items if img.id != image_id]


class _FakeDockerClient:
    def __init__(self, containers: list[_FakeContainer], images: list[_FakeImage]) -> None:
        self.containers = _FakeContainerCollection(containers)
        self.images = _FakeImageCollection(images)


def _install_fake_docker_client(
    provider: DockerProviderInstance,
    containers: list[_FakeContainer],
    images: list[_FakeImage],
) -> _FakeDockerClient:
    """Inject a fake docker client into ``provider`` (cached_property dict slot)."""
    fake = _FakeDockerClient(containers, images)
    provider.__dict__["_docker_client"] = fake
    return fake


def _zero_age_provider(temp_mngr_ctx: MngrContext) -> DockerProviderInstance:
    """Docker provider whose grace window is 0s, so old timestamps suffice for tests."""
    provider = make_docker_provider(temp_mngr_ctx)
    zero_age_cfg = ProviderInstanceConfig(
        backend=ProviderBackendName("docker"),
        min_online_host_age_seconds=0.0,
    )
    new_providers = {**temp_mngr_ctx.config.providers, provider.name: zero_age_cfg}
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, new_providers),
    )
    new_ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, new_config),
    )
    return provider.model_copy_update(
        to_update(provider.field_ref().mngr_ctx, new_ctx),
    )


def test_parse_build_image_host_id_extracts_host_id() -> None:
    """``mngr-build-host-<uuid>:latest`` parses back to its host id."""
    host_id = HostId.generate()
    tag = f"mngr-build-{host_id}:latest"
    parsed = parse_build_image_host_id(tag)
    assert parsed == host_id


def test_parse_build_image_host_id_returns_none_for_snapshot_tag() -> None:
    """Snapshot images use a different tag scheme and must not be picked up."""
    host_id = HostId.generate()
    assert parse_build_image_host_id(f"mngr-snapshot:{host_id}-snap1") is None


def test_parse_build_image_host_id_returns_none_for_unrelated_tag() -> None:
    assert parse_build_image_host_id("debian:bookworm-slim") is None


@pytest.mark.acceptance
def test_gc_orphaned_resources_removes_orphan_container(temp_mngr_ctx: MngrContext) -> None:
    """A labeled mngr-* container whose host id is unknown is force-removed."""
    provider = _zero_age_provider(temp_mngr_ctx)
    orphan_host_id = HostId.generate()
    container = _FakeContainer(
        container_id="cid-orphan-0000",
        name=f"{temp_mngr_ctx.config.prefix}stale-host",
        labels={LABEL_PROVIDER: str(provider.name), LABEL_HOST_ID: str(orphan_host_id)},
        created=_old_iso_timestamp(),
    )
    _install_fake_docker_client(provider, [container], [])

    containers, images = provider.gc_orphaned_resources(known_host_ids=set(), dry_run=False)

    assert container.removed is True
    assert [c.container_id for c in containers] == [container.id]
    assert containers[0].host_id == orphan_host_id
    assert images == []


@pytest.mark.acceptance
def test_gc_orphaned_resources_preserves_live_container(temp_mngr_ctx: MngrContext) -> None:
    """Containers whose host id is in ``known_host_ids`` must be left alone."""
    provider = _zero_age_provider(temp_mngr_ctx)
    live_host_id = HostId.generate()
    container = _FakeContainer(
        container_id="cid-live-0000",
        name=f"{temp_mngr_ctx.config.prefix}live-host",
        labels={LABEL_PROVIDER: str(provider.name), LABEL_HOST_ID: str(live_host_id)},
        created=_old_iso_timestamp(),
    )
    _install_fake_docker_client(provider, [container], [])

    containers, images = provider.gc_orphaned_resources(known_host_ids={live_host_id}, dry_run=False)

    assert container.removed is False
    assert containers == []
    assert images == []


@pytest.mark.acceptance
def test_gc_orphaned_resources_skips_other_provider_containers(temp_mngr_ctx: MngrContext) -> None:
    """Containers whose LABEL_PROVIDER is a different provider are not touched."""
    provider = _zero_age_provider(temp_mngr_ctx)
    container = _FakeContainer(
        container_id="cid-other-0000",
        name=f"{temp_mngr_ctx.config.prefix}other-provider-host",
        labels={LABEL_PROVIDER: "some-other-docker", LABEL_HOST_ID: str(HostId.generate())},
        created=_old_iso_timestamp(),
    )
    _install_fake_docker_client(provider, [container], [])

    containers, _ = provider.gc_orphaned_resources(known_host_ids=set(), dry_run=False)

    assert container.removed is False
    assert containers == []


@pytest.mark.acceptance
def test_gc_orphaned_resources_skips_non_prefixed_container(temp_mngr_ctx: MngrContext) -> None:
    """Containers whose name does not start with the mngr prefix are ignored."""
    provider = _zero_age_provider(temp_mngr_ctx)
    container = _FakeContainer(
        container_id="cid-foreign-0000",
        name="some-unrelated-container",
        labels={},
        created=_old_iso_timestamp(),
    )
    _install_fake_docker_client(provider, [container], [])

    containers, _ = provider.gc_orphaned_resources(known_host_ids=set(), dry_run=False)

    assert container.removed is False
    assert containers == []


@pytest.mark.acceptance
def test_gc_orphaned_resources_grace_period_protects_young_container(temp_mngr_ctx: MngrContext) -> None:
    """Containers younger than ``get_min_online_host_age_seconds`` survive a sweep."""
    # Use the real default (10 minutes) by skipping the zero-age override, then
    # mark the container as just-created.
    provider = make_docker_provider(temp_mngr_ctx)
    young_created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    container = _FakeContainer(
        container_id="cid-young-0000",
        name=f"{temp_mngr_ctx.config.prefix}fresh-host",
        labels={LABEL_PROVIDER: str(provider.name), LABEL_HOST_ID: str(HostId.generate())},
        created=young_created,
    )
    _install_fake_docker_client(provider, [container], [])

    containers, _ = provider.gc_orphaned_resources(known_host_ids=set(), dry_run=False)

    assert container.removed is False
    assert containers == []


@pytest.mark.acceptance
def test_gc_orphaned_resources_dry_run_reports_without_removing(temp_mngr_ctx: MngrContext) -> None:
    """``dry_run=True`` returns the same orphan list but never calls ``remove``."""
    provider = _zero_age_provider(temp_mngr_ctx)
    container = _FakeContainer(
        container_id="cid-dryrun-0000",
        name=f"{temp_mngr_ctx.config.prefix}reportable",
        labels={LABEL_PROVIDER: str(provider.name), LABEL_HOST_ID: str(HostId.generate())},
        created=_old_iso_timestamp(),
    )
    image_host = HostId.generate()
    image = _FakeImage(
        image_id="sha256:imgdryrun",
        tags=[f"mngr-build-{image_host}:latest"],
        created=_old_iso_timestamp(),
        size=1_300_000_000,
    )
    fake_client = _install_fake_docker_client(provider, [container], [image])

    containers, images = provider.gc_orphaned_resources(known_host_ids=set(), dry_run=True)

    assert container.removed is False
    assert fake_client.images.removed_ids == []
    assert [c.container_name for c in containers] == [container.name]
    assert [img.host_id for img in images] == [image_host]


@pytest.mark.acceptance
def test_gc_orphaned_resources_removes_orphan_build_image(temp_mngr_ctx: MngrContext) -> None:
    """``mngr-build-host-*`` images whose host id is unknown are force-removed."""
    provider = _zero_age_provider(temp_mngr_ctx)
    orphan_host = HostId.generate()
    image = _FakeImage(
        image_id="sha256:orphanimg",
        tags=[f"mngr-build-{orphan_host}:latest"],
        created=_old_iso_timestamp(),
        size=1_300_000_000,
    )
    fake = _install_fake_docker_client(provider, [], [image])

    _, images = provider.gc_orphaned_resources(known_host_ids=set(), dry_run=False)

    assert fake.images.removed_ids == [image.id]
    assert [img.host_id for img in images] == [orphan_host]
    assert images[0].size_bytes == 1_300_000_000


@pytest.mark.acceptance
def test_gc_orphaned_resources_preserves_live_build_image(temp_mngr_ctx: MngrContext) -> None:
    """Build images whose host id is live must be retained."""
    provider = _zero_age_provider(temp_mngr_ctx)
    live_host = HostId.generate()
    image = _FakeImage(
        image_id="sha256:liveimg",
        tags=[f"mngr-build-{live_host}:latest"],
        created=_old_iso_timestamp(),
        size=42,
    )
    fake = _install_fake_docker_client(provider, [], [image])

    _, images = provider.gc_orphaned_resources(known_host_ids={live_host}, dry_run=False)

    assert fake.images.removed_ids == []
    assert images == []


@pytest.mark.acceptance
def test_gc_orphaned_resources_ignores_snapshot_images(temp_mngr_ctx: MngrContext) -> None:
    """Snapshot images use a different tag scheme; only build images are reaped."""
    provider = _zero_age_provider(temp_mngr_ctx)
    snapshot_image = _FakeImage(
        image_id="sha256:snapimg",
        tags=[f"mngr-snapshot:{HostId.generate()}-snap1"],
        created=_old_iso_timestamp(),
        size=42,
    )
    fake = _install_fake_docker_client(provider, [], [snapshot_image])

    _, images = provider.gc_orphaned_resources(known_host_ids=set(), dry_run=False)

    assert fake.images.removed_ids == []
    assert images == []


@pytest.mark.acceptance
def test_gc_step_runs_orphan_reconciliation_via_api(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """Top-level ``gc()`` invokes the orphan reconciliation step end-to-end.

    Backs the provider's state volume with a LocalVolume so ``_discover_hosts_for_gc``
    can run without touching a real Docker daemon (it would otherwise reach for the
    state container via the Docker SDK to read host records).
    """
    provider = _zero_age_provider(temp_mngr_ctx)
    provider.__dict__["_state_volume"] = LocalVolume(root_path=tmp_path)
    orphan_host = HostId.generate()
    container = _FakeContainer(
        container_id="cid-api-0000",
        name=f"{temp_mngr_ctx.config.prefix}stale-via-api",
        labels={LABEL_PROVIDER: str(provider.name), LABEL_HOST_ID: str(orphan_host)},
        created=_old_iso_timestamp(),
    )
    image = _FakeImage(
        image_id="sha256:apiimg",
        tags=[f"mngr-build-{orphan_host}:latest"],
        created=_old_iso_timestamp(),
        size=128,
    )
    fake = _install_fake_docker_client(provider, [container], [image])

    result = gc(
        mngr_ctx=temp_mngr_ctx,
        providers=[provider],
        resource_types=GcResourceTypes(is_orphaned_resources=True),
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert result.errors == []
    assert container.removed is True
    assert fake.images.removed_ids == [image.id]
    assert [c.container_id for c in result.containers_destroyed] == [container.id]
    assert [img.image_id for img in result.images_destroyed] == [image.id]


@pytest.mark.acceptance
def test_gc_orphaned_resources_cross_provider_known_ids(temp_mngr_ctx: MngrContext) -> None:
    """A host id known to *any* provider keeps its docker resources alive."""
    docker_provider = _zero_age_provider(temp_mngr_ctx)
    cross_provider_host = HostId.generate()
    container = _FakeContainer(
        container_id="cid-cross-0000",
        name=f"{temp_mngr_ctx.config.prefix}cross-provider-host",
        labels={LABEL_PROVIDER: str(docker_provider.name), LABEL_HOST_ID: str(cross_provider_host)},
        created=_old_iso_timestamp(),
    )
    fake = _install_fake_docker_client(docker_provider, [container], [])

    # Pretend the host id is known to a sibling provider via the
    # ProviderHosts list -- simulate by passing the host id directly.
    hosts_by_provider: ProviderHosts = [
        (
            docker_provider,
            [
                DiscoveredHost(
                    host_id=cross_provider_host,
                    host_name=HostName("cross-provider-host"),
                    provider_name=docker_provider.name,
                    host_state=HostState.RUNNING,
                )
            ],
        )
    ]

    result = GcResult()
    gc_orphaned_resources(
        hosts_by_provider=hosts_by_provider,
        dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
        result=result,
    )

    assert container.removed is False
    assert fake.images.removed_ids == []
    assert result.containers_destroyed == []
