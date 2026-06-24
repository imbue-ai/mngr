import os
import shlex
import shutil
import time
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import TypeVar
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SnapshotNotFoundError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import compute_idle_seconds
from imbue.mngr.hosts.common import determine_lifecycle_state
from imbue.mngr.hosts.common import resolve_expected_process_name
from imbue.mngr.hosts.common import timestamp_to_datetime
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import derive_offline_host_state
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.hosts.offline_host import validate_and_create_discovered_agent
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.cleanup_failures import collect_cleanup_failures
from imbue.mngr.interfaces.cleanup_failures import collecting_cleanup_failures
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import SnapshotRecord
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.listing_utils import build_listing_collection_script
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_vps.bare_realizer import BareRealizer
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import extract_git_depth
from imbue.mngr_vps.build_args import raise_if_vps_migration_arg
from imbue.mngr_vps.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.container_setup import LABEL_HOST_ID
from imbue.mngr_vps.container_setup import check_file_exists_on_outer
from imbue.mngr_vps.container_setup import delete_btrfs_subvolume_on_outer
from imbue.mngr_vps.container_setup import ensure_depot_token_available
from imbue.mngr_vps.container_setup import host_volume_name_for
from imbue.mngr_vps.container_setup import remove_container
from imbue.mngr_vps.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps.container_setup import remove_volume
from imbue.mngr_vps.container_setup import snapshot_trigger_volume_name_for
from imbue.mngr_vps.data_types import PlacementHandle
from imbue.mngr_vps.data_types import RealizePlacementContext
from imbue.mngr_vps.data_types import RealizedPlacement
from imbue.mngr_vps.docker_realizer import CONTAINER_HOST_KEY_NAME
from imbue.mngr_vps.docker_realizer import CONTAINER_KNOWN_HOSTS_NAME
from imbue.mngr_vps.docker_realizer import CONTAINER_SSH_KEY_NAME
from imbue.mngr_vps.docker_realizer import DockerRealizer
from imbue.mngr_vps.errors import BareIsolationNotSupportedError
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.host_setup import MNGR_READY_MARKER_PATH
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.host_store import VpsHostStore
from imbue.mngr_vps.interfaces import HostRealizer
from imbue.mngr_vps.interfaces import SnapshotCapableRealizer
from imbue.mngr_vps.primitives import ISOLATION_TAG_KEY
from imbue.mngr_vps.primitives import IsolationMode
from imbue.mngr_vps.primitives import VPS_HOST_KEY_NAME
from imbue.mngr_vps.primitives import VPS_KNOWN_HOSTS_NAME
from imbue.mngr_vps.primitives import VPS_SSH_KEY_NAME
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import isolation_marker_value
from imbue.mngr_vps.primitives import load_or_create_per_host_host_keypair
from imbue.mngr_vps.primitives import per_host_key_dir
from imbue.mngr_vps.vps_client import VpsClientInterface

ParsedVpsBuildOptionsT = TypeVar("ParsedVpsBuildOptionsT", bound=ParsedVpsBuildOptions)


class _VpsDiscoveryData(FrozenModel):
    """Host records, live agent data, and container running state gathered during discovery.

    Used both for a single VPS read and for the aggregate across all of a
    provider's VPSes; the shapes are identical, so combining is a per-field merge.
    """

    records: tuple[VpsHostRecord, ...] = Field(default=(), description="Discovered host records")
    live_agent_data_by_host_id: dict[HostId, list[dict[str, Any]]] = Field(
        default_factory=dict, description="Live in-container agent data.json dicts keyed by host id"
    )
    is_running_by_host_id: dict[HostId, bool] = Field(
        default_factory=dict, description="Container running state keyed by host id"
    )


def build_vps_tags(
    host_id: HostId, provider_name: str, extra_tags_raw: str, isolation: IsolationMode
) -> dict[str, str]:
    """Compose the tag mapping passed to the VPS create call.

    Always emits ``mngr-host-id=<id>``, ``mngr-provider=<name>`` and
    ``mngr-isolation=<none|container>``. The isolation marker records the host's
    placement on the instance itself, readable from the cloud API without SSH, so
    discovery can pick the realizer matching the host's actual placement before
    opening any on-host store (otherwise a bare host probed by the default
    container realizer is invisible). The ``extra_tags_raw`` string is a
    comma-separated list of ``key=value`` tags that the spawning caller wants
    attached at create time -- e.g. minds-side pool-bake sets
    ``MNGR_VPS_EXTRA_TAGS=minds_env=<name>`` so the env's destroy can later find +
    delete the instance via the Vultr tag filter. Empty / whitespace-only entries
    are skipped so trailing commas don't produce blank tags. Entries without an
    ``=`` are rejected so misconfigured callers fail fast instead of silently
    losing the tag.

    Pulled out to module scope so the comma-splitting behaviour is unit
    testable without standing up an entire provisioning flow.
    """
    tags: dict[str, str] = {
        "mngr-host-id": str(host_id),
        "mngr-provider": provider_name,
        ISOLATION_TAG_KEY: isolation_marker_value(isolation),
    }
    for entry in extra_tags_raw.split(","):
        stripped = entry.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise MngrError(f"Invalid VPS extra tag {stripped!r}: expected ``key=value``")
        key, _, value = stripped.partition("=")
        tags[key.strip()] = value.strip()
    return tags


# HTTP status codes from the VPS provider API that mean the resource the teardown
# step targeted was already gone -- a benign outcome that should not be recorded as
# a cleanup failure.
_VPS_RESOURCE_ALREADY_GONE_STATUS_CODES: Final = (404, 410)


def is_vps_resource_already_gone(error: MngrError) -> bool:
    """Return True iff ``error`` is a VPS API "already gone" (not-found) response.

    Both the Vultr and OVH clients raise ``VpsApiError`` carrying the HTTP
    ``status_code`` (OVH maps its SDK's ``ResourceNotFoundError`` to 404), so we
    classify by that status rather than fragile error-text matching. A real
    failure (the resource exists but could not be destroyed) carries some other
    status and is recorded.
    """
    return isinstance(error, VpsApiError) and error.status_code in _VPS_RESOURCE_ALREADY_GONE_STATUS_CODES


def attempt_cloud_resource_teardown(
    teardown: Callable[[], None],
    *,
    resource_description: str,
    host_id: HostId,
    failures: list[CleanupFailure],
) -> None:
    """Run a single cloud-API teardown step, recording a real cleanup failure if it leaks.

    A failure that means the resource is already gone (HTTP 404/410) is benign and
    dropped; any other ``MngrError`` means a resource that may still exist (and
    incur cost), so it is recorded as a ``HOST_RESOURCE_REMAINS`` failure (the
    aggregation boundary later raises these as a ``CleanupFailedGroup``).
    ``resource_description`` names the leaked resource in the warning + failure
    message (e.g. ``"VPS instance i-123"``).
    """
    try:
        teardown()
    except MngrError as e:
        logger.warning("Failed to tear down {}: {}", resource_description, e)
        if not is_vps_resource_already_gone(e):
            failures.append(
                CleanupFailure(
                    category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                    message=f"failed to tear down {resource_description} for host {host_id}: {e}",
                    host_id=host_id,
                )
            )


def _is_mngr_ready_marker_present_or_none(outer: OuterHostInterface) -> bool | None:
    """Return True if the ``mngr-ready`` marker exists, else None (so polling continues).

    A ``HostConnectionError`` counts as "not ready yet" (None): the bootstrap runs
    ``apt-get install`` and Docker setup which can momentarily disrupt SSH (e.g.
    ``systemctl restart ssh`` after writing the sshd tuning).
    """
    try:
        return True if check_file_exists_on_outer(outer, Path(MNGR_READY_MARKER_PATH)) else None
    except HostConnectionError as e:
        logger.debug("Transient SSH error during host-bootstrap poll (will retry): {}", e)
        return None


def _wait_for_cloud_init_marker(
    outer: OuterHostInterface,
    timeout_seconds: float,
    *,
    poll_interval_seconds: float = 5.0,
    slow_threshold_seconds: float = 30.0,
) -> None:
    """Poll the VPS for the ``mngr-ready`` first-boot completion marker.

    Returns once the marker file appears. The marker is written by whichever
    first-boot mechanism the backend uses -- cloud-init ``runcmd`` (Vultr / AWS /
    OVH) or the GCE ``startup-script`` (GCP). Keeps polling until ``timeout_seconds``
    -- the hard wall (see ``_is_mngr_ready_marker_present_or_none`` for the
    transient-error handling).

    ``poll_interval_seconds`` and ``slow_threshold_seconds`` are parameters so
    tests can drive this with short intervals; defaults preserve the production
    cadence (poll every 5s, warn if total > 30s).
    """
    value, _, elapsed = poll_for_value(
        lambda: _is_mngr_ready_marker_present_or_none(outer),
        timeout=timeout_seconds,
        poll_interval=poll_interval_seconds,
    )
    if value is None:
        raise MngrError(
            f"Cloud-init did not complete within {timeout_seconds}s. Docker may not be installed on the VPS."
        )
    if elapsed > slow_threshold_seconds:
        logger.warning("Host bootstrap took {:.1f}s (threshold: {:.0f}s)", elapsed, slow_threshold_seconds)


class VpsProvider(BaseProviderInstance):
    """Provider that places one agent on each VPS instance (1:1 host:VPS).

    The substrate owns the machine (provisioning, boot, destroy); a selected
    ``HostRealizer`` (``config.isolation``) owns how the agent sits on it. The
    container realizer (the default) runs the agent in a Docker container on a
    VPS that stays running, so stop/start acts on the container; the bare
    realizer runs the agent directly on the VM OS, so stop/start acts on the
    machine. Destroying the host removes both the placement and the VPS.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: VpsProviderConfig = Field(frozen=True, description="VPS provider configuration")
    vps_client: VpsClientInterface = Field(frozen=True, description="VPS provider API client")

    _host_record_cache: dict[HostId, VpsHostRecord] = PrivateAttr(default_factory=dict)
    _instances_cache: list[dict[str, Any]] | None = PrivateAttr(default=None)
    _realizer_cache: dict[IsolationMode, HostRealizer] = PrivateAttr(default_factory=dict)

    def _realizer_for_isolation(self, isolation: IsolationMode) -> HostRealizer:
        """Construct (and cache) the realizer for a given placement.

        Parameterized by ``isolation`` rather than reading ``config.isolation``
        directly so a single provider can serve both placements: ``config.isolation``
        selects the realizer for NEWLY-CREATED hosts (``self._realizer``), while
        operations on an EXISTING host resolve that host's own placement (from its
        instance marker or its record) and use the matching realizer. The default
        ``CONTAINER`` preserves the original behavior.
        """
        cached = self._realizer_cache.get(isolation)
        if cached is not None:
            return cached
        match isolation:
            case IsolationMode.CONTAINER:
                realizer: HostRealizer = DockerRealizer(
                    config=self.config,
                    mngr_ctx=self.mngr_ctx,
                    key_dir=self._key_dir(),
                    host_dir=self.host_dir,
                    provider_name=self.name,
                )
            case IsolationMode.NONE:
                realizer = BareRealizer(
                    config=self.config,
                    mngr_ctx=self.mngr_ctx,
                    key_dir=self._key_dir(),
                    host_dir=self.host_dir,
                    provider_name=self.name,
                )
            case _ as unreachable:
                assert_never(unreachable)
        self._realizer_cache[isolation] = realizer
        return realizer

    @property
    def _realizer(self) -> HostRealizer:
        """The CREATE-time placement realizer, selected by ``config.isolation``.

        Use this only on the create path. Operations on an existing host must use
        ``_realizer_for_record`` (the host's recorded placement) so a bare host is
        reachable even when the provider config defaults to container, and vice
        versa.
        """
        return self._realizer_for_isolation(self.config.isolation)

    def _realizer_for_record(self, record: VpsHostRecord) -> HostRealizer:
        """The realizer matching an EXISTING host's recorded placement.

        A bare host's record has ``config.container_name is None`` (there is no
        container); a container host names its container. A record predating the
        bare placement always named a container, so ``container_name is None``
        reliably means bare. When the record carries no config yet (not finalized),
        fall back to the create-time realizer.
        """
        if record.config is None:
            return self._realizer
        is_bare = record.config.container_name is None
        return self._realizer_for_isolation(IsolationMode.NONE if is_bare else IsolationMode.CONTAINER)

    def _realizer_for_vps_ip(self, vps_ip: str) -> HostRealizer:
        """The realizer to probe the host on ``vps_ip`` with, BEFORE its record is read.

        Discovery has only the VPS IP -- not yet the host record (reading it
        requires knowing the placement: a bare host's record lives at a fixed
        root-disk path, a container host's inside its per-host docker volume). The
        base provider has no SSH-free instance listing and is container-only on the
        bare-rejecting providers, so it returns the create-time realizer
        (``config.isolation``); ``OfflineCapableVpsProvider`` overrides this to read
        the host's placement from the instance's ``mngr-isolation`` marker, so a
        bare host is probed with the bare realizer even under a default-container
        config.
        """
        del vps_ip
        return self._realizer

    @property
    def supports_snapshots(self) -> bool:
        """Whether this provider's CREATE-time placement can snapshot.

        A provider-capability advertisement keyed off ``config.isolation`` (the
        container realizer snapshots; the bare realizer does not). Per-host
        snapshot operations narrow the host's own realizer instead, via
        ``_require_snapshot_capable_realizer``.
        """
        return isinstance(self._realizer, SnapshotCapableRealizer)

    def _require_snapshot_capable_realizer(self, realizer: HostRealizer) -> SnapshotCapableRealizer:
        """Narrow ``realizer`` to a snapshot-capable one, or raise up front if it is not."""
        if isinstance(realizer, SnapshotCapableRealizer):
            return realizer
        raise SnapshotsNotSupportedError(self.name)

    @property
    def supports_shutdown_hosts(self) -> bool:
        return True

    @property
    def supports_volumes(self) -> bool:
        return True

    @property
    def supports_mutable_tags(self) -> bool:
        return False

    @property
    def _supports_bare_isolation(self) -> bool:
        """Whether this provider can run ``isolation=NONE`` (bare) placements.

        Default False: bare needs a machine stop/start lifecycle (the idle agent
        powers the VM off and ``mngr start`` boots it again). Providers with that
        substrate -- aws/gcp/azure -- override this to True.
        """
        return False

    def reset_caches(self) -> None:
        for host_id in list(self._host_by_id_cache):
            self._evict_cached_host(host_id)
        self._host_record_cache.clear()
        self._instances_cache = None

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """Provider-specific listing hook: return the raw instance dicts from the API.

        Default returns ``[]`` so subclasses without a tag-based listing API
        (e.g. OVH, which uses ``_list_provider_vps_hostnames`` directly) can
        opt out. Subclasses with one (currently AWS and Vultr) override to
        call their typed client's ``list_instances``; the result is cached
        for the duration of a single command via ``_list_instances_cached``.
        """
        return []

    def _list_instances_cached(self) -> list[dict[str, Any]]:
        """List instances tagged for this provider, caching for the duration of the command.

        Subclasses customise *what* gets listed by overriding
        ``_fetch_provider_instances``; the cache scaffolding lives here.
        """
        if self._instances_cache is not None:
            return self._instances_cache
        self._instances_cache = self._fetch_provider_instances()
        return self._instances_cache

    def _validate_provider_args_for_create(self) -> None:
        """Hook called by ``create_host`` before the first provider write.

        Default no-op. Subclasses override to enforce provider-specific
        pre-create invariants (e.g. GCP's firewall-rule existence, AWS's
        pytest-only "must have auto_shutdown_seconds set" guard). It runs before
        any provider API write -- in particular before the SSH key upload and
        instance creation -- so a failed precondition surfaces cleanly with no
        leaked resources and no cleanup path. Keep these checks cheap (local
        state or a single read-only API call); anything expensive runs on every
        ``mngr create``.
        """

    def _validate_external_store_ready(self) -> None:
        """Hook called by ``create_host`` before the first provider write.

        Default no-op. ``OfflineCapableVpsProvider`` overrides it to fail fast
        when its required offline state store has not been provisioned, so a
        missing object-storage bucket fails ``mngr create`` cleanly *before* any
        instance is launched (rather than after, when the host record write would
        otherwise hit the absent store with an instance already running). Kept
        separate from ``_validate_provider_args_for_create`` so it needs no
        per-provider ``super()`` coordination.
        """

    # =========================================================================
    # Key Management
    # =========================================================================

    def _key_dir(self) -> Path:
        """Directory for SSH keys for this provider instance."""
        key_dir = self.mngr_ctx.profile_dir / "providers" / str(self.config.backend) / str(self.name) / "keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        return key_dir

    def _get_vps_ssh_keypair(self) -> tuple[Path, str]:
        """Load or create the SSH keypair for authenticating to the VPS."""
        return load_or_create_ssh_keypair(self._key_dir(), VPS_SSH_KEY_NAME)

    def _get_container_ssh_keypair(self) -> tuple[Path, str]:
        """Load or create the SSH keypair for authenticating to the container.

        Kept on the provider (delegating to the same key-file name the
        ``DockerRealizer`` uses) so the imbue_cloud slice provider's
        ``_create_host_object`` override keeps reaching the container keypair.
        """
        return load_or_create_ssh_keypair(self._key_dir(), CONTAINER_SSH_KEY_NAME)

    def _get_vps_host_keypair(self, host_id: HostId) -> tuple[Path, str]:
        """Load or create this host's unique Ed25519 host keypair (injected into the VPS via cloud-init)."""
        return load_or_create_per_host_host_keypair(self._key_dir(), host_id, VPS_HOST_KEY_NAME)

    def _get_container_host_keypair(self, host_id: HostId) -> tuple[Path, str]:
        """Load or create this host's unique Ed25519 host keypair for the container's sshd."""
        return load_or_create_per_host_host_keypair(self._key_dir(), host_id, CONTAINER_HOST_KEY_NAME)

    def get_ssh_host_public_keys(self, host_id: HostId) -> tuple[str | None, str | None]:
        # Both host keys are generated by us at bake time (the VPS/VM-root key via
        # cloud-init or the lima YAML; the container key injected into the
        # container's sshd), unique per host, so the public halves are known
        # deterministically with no scan. Surfaced via ``mngr create --format
        # json`` for the pool bake to persist and pin. Called only after the keys
        # have been created (create JSON / rebuild), so the per-host keys exist.
        _vps_host_key_path, vps_host_public_key = self._get_vps_host_keypair(host_id)
        _container_host_key_path, container_host_public_key = self._get_container_host_keypair(host_id)
        return (vps_host_public_key, container_host_public_key)

    def _vps_known_hosts_path(self) -> Path:
        return self._key_dir() / VPS_KNOWN_HOSTS_NAME

    def record_outer_host_key(self, host: str, port: int, public_key: str) -> None:
        """Pin an outer (VPS-root) sshd host key in this provider's known_hosts.

        Callers operating on a VPS this provider did not itself order (e.g. the
        imbue_cloud rebuild on a leased host) use this so the provider's own outer
        connections -- including the certified-data sync callback -- pass strict
        host-key checking instead of failing on a missing entry.
        """
        add_host_to_known_hosts(
            known_hosts_path=self._vps_known_hosts_path(),
            hostname=host,
            port=port,
            public_key=public_key,
        )

    def _container_known_hosts_path(self) -> Path:
        return self._key_dir() / CONTAINER_KNOWN_HOSTS_NAME

    # =========================================================================
    # Outer host helper
    # =========================================================================

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
        """Open an outer host targeting root@vps_ip:22 via the provider's VPS SSH key.

        Use this during create_host (when host_id is not yet known); use
        ``outer_host_for(host_id)`` once a host record exists.
        """
        vps_key_path, _pub = self._get_vps_ssh_keypair()
        pyinfra_host = create_pyinfra_host(
            hostname=vps_ip,
            port=22,
            private_key_path=vps_key_path,
            known_hosts_path=self._vps_known_hosts_path(),
            ssh_user="root",
        )
        outer = OuterHost(
            id=HostId.generate(),
            connector=PyinfraConnector(pyinfra_host),
            mngr_ctx=self.mngr_ctx,
        )
        try:
            yield outer
        finally:
            outer.disconnect()

    # =========================================================================
    # Host Store
    # =========================================================================
    # The substrate opens the store via ``self._realizer.open_host_store(outer,
    # host_id)``; where the ``host_state.json`` + ``agents/`` layout physically
    # lives is the realizer's concern (the container realizer resolves the
    # per-host docker volume's bind-source path; the bare realizer points at a
    # fixed root-disk directory).

    # =========================================================================
    # Host Object Construction
    # =========================================================================

    def _create_host_object(
        self,
        host_id: HostId,
        host_name: HostName,
        vps_ip: str,
        realizer: HostRealizer,
    ) -> Host:
        """Create a Host object with direct SSH to the agent placement on the VPS.

        ``realizer`` is the placement realizer for THIS host (the create-time
        realizer on create; the host's recorded placement for an existing host).
        It decides where the agent sshd lives (container realizer:
        ``vps_ip:container_ssh_port`` with the container keypair; bare realizer:
        the VM's own port-22 sshd).
        """
        endpoint = realizer.agent_endpoint(vps_ip)
        pyinfra_host = create_pyinfra_host(
            hostname=endpoint.hostname,
            port=endpoint.port,
            private_key_path=endpoint.private_key_path,
            known_hosts_path=endpoint.known_hosts_path,
            ssh_user=endpoint.ssh_user if endpoint.ssh_user is not None else "root",
        )

        connector = PyinfraConnector(pyinfra_host)
        host = Host(
            id=host_id,
            host_name=host_name,
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data, vps_ip, realizer
            ),
        )
        self._evict_cached_host(host_id, replacement=host)
        return host

    def _create_offline_host(
        self,
        host_record: VpsHostRecord,
    ) -> OfflineHost:
        """Create an OfflineHost from a host record.

        Wrapped so the offline host is readable (file reads served from its
        persisted volume) whether reached via ``get_host`` or
        ``to_offline_host``; the volume is resolved lazily, so this is free.
        """
        host_id = HostId(host_record.certified_host_data.host_id)
        vps_ip = host_record.vps_ip or ""
        realizer = self._realizer_for_record(host_record)
        offline = make_readable_offline_host(
            OfflineHost(
                id=host_id,
                certified_host_data=host_record.certified_host_data,
                provider_instance=self,
                mngr_ctx=self.mngr_ctx,
                on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                    callback_host_id, certified_data, vps_ip, realizer
                ),
            )
        )
        self._evict_cached_host(host_id, replacement=offline)
        return offline

    def _on_certified_host_data_updated(
        self, host_id: HostId, certified_data: CertifiedHostData, vps_ip: str, realizer: HostRealizer
    ) -> None:
        """Callback when host data.json is updated -- sync to the unified host volume.

        ``realizer`` is the placement realizer for this host (captured when the
        host object was created), so the store is opened against the host's actual
        placement rather than the provider's create-time default.
        """
        try:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                host_store = realizer.open_host_store(outer, host_id)
                existing = host_store.read_host_record()
                if existing is not None:
                    updated = existing.model_copy_update(
                        to_update(existing.field_ref().certified_host_data, certified_data)
                    )
                    self._write_and_mirror(host_store, updated)
        except MngrError as e:
            logger.warning("Failed to sync certified data to VPS host volume: {}", e)

    # =========================================================================
    # VPS Provisioning
    # =========================================================================

    def _wait_for_cloud_init(self, outer: OuterHostInterface, timeout_seconds: float) -> None:
        """Wait for cloud-init to finish (Docker installed, marker file present)."""
        _wait_for_cloud_init_marker(outer, timeout_seconds=timeout_seconds)

    def _wait_for_sshd_on_vps(self, vps_ip: str, timeout_seconds: float) -> None:
        """Wait for sshd on the VPS to be ready."""
        wait_for_sshd(hostname=vps_ip, port=22, timeout_seconds=timeout_seconds)

    # =========================================================================
    # Core Lifecycle: create_host
    # =========================================================================

    def create_host(
        self,
        name: HostName,
        image: ImageReference | None = None,
        tags: Mapping[str, str] | None = None,
        build_args: Sequence[str] | None = None,
        start_args: Sequence[str] | None = None,
        lifecycle: HostLifecycleOptions | None = None,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
        snapshot: SnapshotName | None = None,
    ) -> Host:
        host_id = HostId.generate()
        logger.info("Creating VPS host {} ({}) ...", name, host_id)

        # Bare placement needs a substrate that can stop and restart the machine
        # (the idle agent powers the VM off); reject it up front on providers
        # that would strand the VM, before any billable provisioning.
        if self.config.isolation is IsolationMode.NONE and not self._supports_bare_isolation:
            raise BareIsolationNotSupportedError(
                f"Provider {self.name!r} does not support isolation=NONE (bare placement); "
                "it has no machine stop/start lifecycle, so a bare agent would strand the VM. "
                "Use isolation=CONTAINER, or an aws/gcp/azure provider for bare."
            )

        parsed = self._parse_build_args(build_args)

        # A bare placement runs the agent on the VM's OS, with no Docker image or
        # container. Reject container-only create inputs up front rather than
        # silently ignore them: an image override, a Dockerfile build, or docker
        # run start-args all have no effect on a bare host.
        if self.config.isolation is IsolationMode.NONE:
            container_only = [
                label
                for present, label in (
                    (image is not None, "an image override"),
                    (bool(parsed.docker_build_args), "Docker build args"),
                    (bool(start_args), "start args (docker run flags)"),
                )
                if present
            ]
            if container_only:
                raise MngrError(
                    f"isolation=NONE (bare placement) does not support {', '.join(container_only)}: "
                    "a bare host runs the agent on the VM's OS with no Docker image or container. "
                    "Remove these inputs, or use isolation=CONTAINER."
                )

        # Fail fast before provisioning a (billable) VPS: a DEPOT build needs
        # DEPOT_TOKEN, but the build only runs after the VPS exists and
        # cloud-init completes -- so a missing token would otherwise waste a
        # full provision. Only an actual build (non-empty docker_build_args)
        # needs the token; a plain image pull does not.
        if parsed.docker_build_args:
            ensure_depot_token_available(self.config.builder)

        # Provider-specific pre-create checks (e.g. GCP's firewall-rule
        # existence, AWS's pytest auto-shutdown guard) plus the offline
        # state-store readiness check. Both run before the first provider write
        # (the SSH key upload just below) so a failed precondition -- a missing
        # `mngr gcp prepare` firewall rule, or an unprovisioned object-storage
        # state bucket -- surfaces cleanly: no instance created, no SSH key
        # uploaded, and no "Host creation failed, attempting cleanup..." path.
        self._validate_provider_args_for_create()
        self._validate_external_store_ready()

        _vps_key_path, vps_public_key = self._get_vps_ssh_keypair()
        vps_host_key_path, vps_host_public_key = self._get_vps_host_keypair(host_id)

        with log_span("Uploading SSH key to provider"):
            key_name = f"mngr-{self.name}-{host_id}"
            vps_ssh_key_id = self.vps_client.upload_ssh_key(key_name, vps_public_key)

        vps_instance_id: VpsInstanceId | None = None
        vps_ip: str | None = None
        try:
            vps_instance_id, vps_ip = self._provision_vps(
                host_id=host_id,
                name=name,
                parsed=parsed,
                vps_host_key_path=vps_host_key_path,
                vps_host_public_key=vps_host_public_key,
                vps_ssh_key_id=vps_ssh_key_id,
                vps_public_key=vps_public_key,
            )

            with self._make_outer_for_vps_ip(vps_ip) as outer:
                host = self.create_host_on_existing_vps(
                    outer=outer,
                    host_id=host_id,
                    name=name,
                    vps_ip=vps_ip,
                    vps_instance_id=vps_instance_id,
                    vps_ssh_key_id=vps_ssh_key_id,
                    vps_host_public_key=vps_host_public_key,
                    region=parsed.region,
                    plan=parsed.plan,
                    image=image,
                    tags=tags,
                    build_args=build_args,
                    start_args=start_args,
                    lifecycle=lifecycle,
                    known_hosts=known_hosts,
                    authorized_keys=authorized_keys,
                )

            logger.info("VPS host {} created successfully (VPS: {}, IP: {})", name, vps_instance_id, vps_ip)
            return host

        except Exception:
            keep_failed = os.environ.get("MNGR_KEEP_FAILED_HOSTS", "0") == "1"
            if keep_failed:
                logger.error(
                    "Host creation failed. MNGR_KEEP_FAILED_HOSTS=1 is set, "
                    "skipping cleanup so you can debug. VPS instance: {}, IP: {}",
                    vps_instance_id,
                    vps_ip,
                )
            else:
                logger.error("Host creation failed, attempting cleanup...")
                try:
                    if vps_instance_id is not None:
                        self.vps_client.destroy_instance(vps_instance_id)
                except Exception as cleanup_err:
                    logger.warning("Failed to clean up VPS instance: {}", cleanup_err)
                try:
                    self.vps_client.delete_ssh_key(vps_ssh_key_id)
                except Exception as cleanup_err:
                    logger.warning("Failed to clean up SSH key: {}", cleanup_err)
            raise

    def create_host_on_existing_vps(
        self,
        *,
        outer: OuterHostInterface,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        vps_instance_id: VpsInstanceId,
        vps_ssh_key_id: str,
        vps_host_public_key: str,
        region: str,
        plan: str,
        image: ImageReference | None,
        tags: Mapping[str, str] | None,
        build_args: Sequence[str] | None,
        start_args: Sequence[str] | None,
        lifecycle: HostLifecycleOptions | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
    ) -> Host:
        """Build the container and finalize host state on an already-reachable VPS.

        This is the single canonical "set up the host after the VPS exists"
        code path. ``create_host`` calls it once it has ordered (or recycled)
        a VPS and opened an outer; other providers that operate on a VPS they
        did not order themselves (e.g. ``mngr_imbue_cloud``'s slow path, which
        rebuilds the container on a leased pool VPS) call it directly with
        their own ``outer``. It makes no VPS-client (ordering) calls.

        The realizer provisions the unified host volume at the top of
        ``realize_placement`` -- before the (potentially slow and failure-prone)
        image pull/build -- so the volume still exists by the time
        ``_finalize_host_creation`` writes ``host_state.json``.
        """
        base_image = str(image) if image else self.config.default_image
        # Prepend `--runtime <value>` (e.g. 'runsc' for gVisor) when configured; absent by default.
        runtime_args = ("--runtime", self.config.docker_runtime) if self.config.docker_runtime is not None else ()
        effective_start_args = runtime_args + tuple(self.config.default_start_args) + tuple(start_args or ())
        parsed = self._parse_build_args(build_args)

        realized = self._realizer.realize_placement(
            outer,
            RealizePlacementContext(
                host_id=host_id,
                name=name,
                vps_ip=vps_ip,
                base_image=base_image,
                effective_start_args=effective_start_args,
                docker_build_args=parsed.docker_build_args,
                git_depth=parsed.git_depth,
                tags=tags,
                known_hosts=known_hosts,
                authorized_keys=authorized_keys,
            ),
        )

        # Wait for the agent sshd here (not in the realizer) so subclasses can
        # override ``_wait_for_container_sshd`` to wait on a dynamically
        # forwarded port (the imbue_cloud slice provider does this).
        logger.log(LogLevel.BUILD.value, "Waiting for agent SSH to be ready...", source="vps")
        with log_span("Waiting for agent SSH"):
            self._wait_for_container_sshd(vps_ip)
        logger.log(LogLevel.BUILD.value, "Agent SSH ready", source="vps")

        return self._finalize_host_creation(
            host_id=host_id,
            name=name,
            vps_ip=vps_ip,
            outer=outer,
            realized=realized,
            base_image=base_image,
            effective_start_args=effective_start_args,
            tags=tags,
            lifecycle=lifecycle,
            region=region,
            plan=plan,
            vps_instance_id=vps_instance_id,
            vps_ssh_key_id=vps_ssh_key_id,
            vps_host_public_key=vps_host_public_key,
        )

    def teardown_container_on_existing_vps(self, outer: OuterHostInterface, host_id: HostId) -> None:
        """Remove the container + per-host volumes/subvolume for ``host_id`` on a reachable VPS.

        The VPS itself is left running (no VPS-client calls). Used to clear a
        pre-existing container before rebuilding on the same VPS -- e.g. the
        imbue_cloud slow path reclaiming a leased pool host whose baked
        container must be torn down before ``create_host_on_existing_vps``
        rebuilds it under the same ``host_id``. Each step is best-effort and
        logged; a missing resource is a no-op.
        """
        # Remove every workspace container identified by its host-id label.
        list_result = outer.execute_idempotent_command(
            f"docker ps -aq --filter label={LABEL_HOST_ID}={shlex.quote(str(host_id))}"
        )
        if list_result.success:
            for container_id in list_result.stdout.split():
                try:
                    remove_container(outer, container_id, force=True)
                except (HostConnectionError, MngrError) as e:
                    logger.warning("Failed to remove container {} for host {}: {}", container_id, host_id, e)
        else:
            logger.warning("Failed to list containers for host {}: {}", host_id, list_result.stderr.strip())

        # Delete the per-host btrfs subvolume (the bind source for the unified
        # volume) so the rebuild can recreate it cleanly.
        subvolume_path = self.config.btrfs_mount_path / host_id.get_uuid().hex
        try:
            delete_btrfs_subvolume_on_outer(outer, subvolume_path)
        except (HostConnectionError, MngrError) as e:
            logger.warning("Failed to delete btrfs subvolume {}: {}", subvolume_path, e)

        # Remove the named docker volumes so a recreate with the same names
        # doesn't collide.
        for volume_name in (host_volume_name_for(host_id), snapshot_trigger_volume_name_for(host_id)):
            try:
                remove_volume(outer, volume_name)
            except (HostConnectionError, MngrError) as e:
                logger.warning("Failed to remove volume {} for host {}: {}", volume_name, host_id, e)

    def _require_parsed(
        self, parsed: ParsedVpsBuildOptions, expected_cls: type[ParsedVpsBuildOptionsT]
    ) -> ParsedVpsBuildOptionsT:
        """Narrow ``parsed`` to the provider's expected build-options subclass, or raise uniformly.

        Each provider's ``_create_vps_instance`` override needs the concrete
        per-provider build options (e.g. ``ParsedAwsBuildOptions``) so its extra
        knobs are statically visible. ``_parse_build_args`` always returns that
        shape, so a mismatch indicates the parser hook returned the wrong type.
        """
        if isinstance(parsed, expected_cls):
            return parsed
        raise MngrError(
            f"{type(self).__name__}._create_vps_instance expected {expected_cls.__name__}, "
            f"got {type(parsed).__name__}. This indicates the parser hook returned a mismatched "
            f"shape; _parse_build_args must return {expected_cls.__name__}."
        )

    def _create_vps_instance(
        self,
        parsed: ParsedVpsBuildOptions,
        label: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        """Provider hook that issues the cloud-API instance create.

        Default implementation: call the shared ``self.vps_client.create_instance``
        with the standard (label, region, plan, user_data, ssh_key_ids, tags)
        shape, which is what every current backend's wire contract needs.

        Providers that have per-host knobs beyond region + plan (e.g. AWS's
        ``--aws-ami=`` override) override this hook to pass their extra kwargs
        through their own concrete typed client (``self.aws_client`` rather
        than the shared interface). That way the shared ``VpsClientInterface``
        contract stays minimal and per-provider extensions don't ripple across
        every other provider's signature.
        """
        return self.vps_client.create_instance(
            label=label,
            region=parsed.region,
            plan=parsed.plan,
            user_data=user_data,
            ssh_key_ids=ssh_key_ids,
            tags=tags,
        )

    def _generate_bootstrap_payload(
        self,
        *,
        host_private_key: str,
        host_public_key: str,
        authorized_user_public_key: str,
    ) -> str:
        """Render the first-boot bootstrap payload threaded into instance creation.

        Default: cloud-init ``user-data``. GCP overrides this to render a GCE
        ``startup-script`` (stock GCE images run the google-guest-agent, not
        cloud-init). Both render the same shared ``host_setup`` steps and write the
        same ``mngr-ready`` marker, so the rest of provisioning is backend-agnostic.
        """
        return generate_cloud_init_user_data(
            host_private_key=host_private_key,
            host_public_key=host_public_key,
            install_gvisor_runtime=self.config.install_gvisor_runtime,
            auto_shutdown_seconds=self._get_effective_auto_shutdown_seconds(),
            authorized_user_public_key=authorized_user_public_key,
        )

    def _wait_for_expected_host_key(self, vps_ip: str, expected_host_public_key: str, timeout_seconds: float) -> None:
        """Hook to wait until the VPS serves our SSH host key before strict-checking.

        Default no-op: cloud-init backends set the host key before sshd starts.
        Backends that set it after boot (GCP's ``startup-script``) override this to
        poll until the live key matches, closing the mismatch window.
        """
        return

    def _provision_vps(
        self,
        host_id: HostId,
        name: HostName,
        parsed: ParsedVpsBuildOptions,
        vps_host_key_path: Path,
        vps_host_public_key: str,
        vps_ssh_key_id: str,
        vps_public_key: str,
    ) -> tuple[VpsInstanceId, str]:
        """Provision a VPS, wait for it to boot, and wait for Docker to install.

        Returns (vps_instance_id, vps_ip).

        ``vps_public_key`` is the provider SSH public key already loaded by
        ``create_host`` (the sole caller); it is threaded in rather than re-read
        from disk here. The bootstrap (``_generate_bootstrap_payload``) injects it
        straight into root (in addition to the copy-from-default-user step), which
        removes any reliance on a cloud image's default-user key landing in root --
        notably on GCE, where the google guest agent provisions the key
        asynchronously and races the copy.

        Provider-specific pre-create checks (``_validate_provider_args_for_create``)
        already ran in ``create_host`` before the first provider write, so by the
        time we get here the create preconditions are known to hold.
        """
        vps_host_private_key = vps_host_key_path.read_text()
        user_data = self._generate_bootstrap_payload(
            host_private_key=vps_host_private_key,
            host_public_key=vps_host_public_key,
            authorized_user_public_key=vps_public_key,
        )

        logger.log(
            LogLevel.BUILD.value,
            "Creating VPS instance (region: {}, plan: {})...",
            parsed.region,
            parsed.plan,
            source="vps",
        )
        with log_span("Creating VPS instance"):
            vps_tags = build_vps_tags(
                host_id, self.name, os.environ.get("MNGR_VPS_EXTRA_TAGS", ""), self.config.isolation
            )
            vps_instance_id = self._create_vps_instance(
                parsed=parsed,
                label=f"mngr-{name}",
                user_data=user_data,
                ssh_key_ids=[vps_ssh_key_id],
                tags=vps_tags,
            )

        logger.log(LogLevel.BUILD.value, "Waiting for VPS to become active...", source="vps")
        with log_span("Waiting for VPS to become active"):
            vps_ip = self.vps_client.wait_for_instance_active(
                vps_instance_id,
                timeout_seconds=self.config.instance_boot_timeout,
            )
        logger.log(LogLevel.BUILD.value, "VPS active (IP: {})", vps_ip, source="vps")

        add_host_to_known_hosts(
            known_hosts_path=self._vps_known_hosts_path(),
            hostname=vps_ip,
            port=22,
            public_key=vps_host_public_key,
        )

        logger.log(LogLevel.BUILD.value, "Waiting for SSH to be ready on VPS...", source="vps")
        with log_span("Waiting for VPS SSH"):
            self._wait_for_sshd_on_vps(vps_ip, timeout_seconds=self.config.ssh_connect_timeout)

        # Backends that install the host key after sshd starts (GCP's
        # startup-script) serve a boot-generated key first; wait for our key to
        # land before the strict-host-key-checked connection below. No-op for
        # cloud-init backends, which set the key pre-sshd.
        with log_span("Waiting for expected VPS host key"):
            self._wait_for_expected_host_key(
                vps_ip, vps_host_public_key, timeout_seconds=self.config.ssh_connect_timeout
            )

        logger.log(
            LogLevel.BUILD.value, "Waiting for host bootstrap to complete (Docker installation)...", source="vps"
        )
        with log_span("Waiting for host bootstrap (Docker install)"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                self._wait_for_cloud_init(outer, timeout_seconds=self.config.docker_install_timeout)
        logger.log(LogLevel.BUILD.value, "Host bootstrap complete, Docker is ready", source="vps")

        return vps_instance_id, vps_ip

    def _finalize_host_creation(
        self,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        outer: OuterHostInterface,
        realized: RealizedPlacement,
        base_image: str,
        effective_start_args: tuple[str, ...],
        tags: Mapping[str, str] | None,
        lifecycle: HostLifecycleOptions | None,
        region: str,
        plan: str,
        vps_instance_id: VpsInstanceId,
        vps_ssh_key_id: str,
        vps_host_public_key: str,
    ) -> Host:
        """Create the Host object, configure activity watching, and persist state."""
        # The container realizer fills the handle in; a bare placement's handle is
        # empty, so these are None (the matching ``VpsHostConfig`` fields are
        # nullable to represent that).
        handle = realized.handle
        host = self._create_host_object(host_id, name, vps_ip, self._realizer)

        lifecycle_options = lifecycle if lifecycle is not None else HostLifecycleOptions()
        activity_config = lifecycle_options.to_activity_config(
            default_idle_timeout_seconds=self.config.default_idle_timeout,
            default_idle_mode=self.config.default_idle_mode,
            default_activity_sources=self.config.default_activity_sources,
        )

        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(name),
            idle_timeout_seconds=activity_config.idle_timeout_seconds,
            activity_sources=activity_config.activity_sources,
            image=base_image,
            user_tags=dict(tags) if tags else {},
            created_at=now,
            updated_at=now,
        )
        host.record_activity(ActivitySource.BOOT)
        host.set_certified_data(host_data)

        self._create_shutdown_script(host)
        with log_span("Starting activity watcher"):
            self._realizer.start_activity_watcher(outer, handle)

        host_record = VpsHostRecord(
            certified_host_data=host_data,
            vps_ip=vps_ip,
            ssh_host_public_key=vps_host_public_key,
            container_ssh_host_public_key=realized.container_ssh_host_public_key,
            config=VpsHostConfig(
                vps_instance_id=vps_instance_id,
                region=region,
                plan=plan,
                start_args=effective_start_args,
                image=base_image,
                container_name=handle.container_name,
                volume_name=handle.volume_name,
                vps_ssh_key_id=vps_ssh_key_id,
            ),
            container_id=handle.container_id,
        )
        host_store = self._realizer.open_host_store(outer, host_id)
        self._write_and_mirror(host_store, host_record)

        # Cache so that persist_agent_data (called moments later) can find
        # the record without re-querying the Vultr API, which would return
        # a stale instance list that doesn't include the VPS we just created.
        self._host_record_cache[host_id] = host_record

        self._on_host_finalized(host_id=host_id, vps_ip=vps_ip)

        return host

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Hook called at the very end of ``_finalize_host_creation``.

        Fires after the host record has been written to the unified host
        volume and is therefore the "point of no return" for ``create_host``.
        Subclasses can override to commit any deferred provisioning side
        effects that must only become durable once the host is fully
        usable -- e.g. OVH classic VPS un-cancellation, which must wait
        until container setup has succeeded so that a failure earlier in
        the flow lets the VPS auto-decommission instead of leaking
        a still-billing orphan.

        Default no-op. An override may raise only when ``create_host``'s
        cleanup can reverse the partially-created host (e.g. the offline
        host_dir-sync install in ``OfflineCapableVpsProvider``, where a setup
        failure must fail create); an override whose side effect is NOT undone
        by that cleanup (e.g. OVH un-cancellation, which would leak a
        still-billing VPS) must instead catch and log its own errors.
        """

    def _persist_host_record_externally(self, record: VpsHostRecord) -> None:
        """Mirror the authoritative on-volume host record to an external store.

        Lets a provider copy the record to a compute-decoupled object store
        (e.g. an S3 bucket) so a stopped/offline instance's full record stays
        readable without SSH. Called right after every on-volume
        ``write_host_record``. Default no-op, so providers without an external
        store are unaffected.
        """

    def _write_and_mirror(self, host_store: VpsHostStore, record: VpsHostRecord) -> None:
        """Write the authoritative on-volume record *and* mirror it to the external store.

        Every on-volume ``write_host_record`` must be paired with
        ``_persist_host_record_externally`` so the offline mirror never lags the
        on-volume record (a past bug let the two drift apart). Routing both through
        this one method makes that pairing structural.
        """
        host_store.write_host_record(record)
        self._persist_host_record_externally(record)

    def _delete_host_record_externally(self, host_id: HostId) -> None:
        """Remove a host's record from the external store, if any.

        The inverse of ``_persist_host_record_externally``: called when a host
        is destroyed/deleted so its mirrored record does not linger in the
        external store. Default no-op.
        """

    def _wait_for_container_sshd(self, vps_ip: str, realizer: HostRealizer | None = None) -> None:
        """Wait for the agent's sshd to be reachable at the realizer's endpoint port.

        Container realizer: the exposed container port; bare realizer: the VM's
        own port 22. ``realizer`` defaults to the create-time realizer (the create
        path); ``start_host`` passes the existing host's own placement realizer so
        a bare host waits on port 22 even under a default-container config. (The
        imbue_cloud slice provider overrides this to wait on a dynamically
        forwarded port instead.)
        """
        effective_realizer = realizer if realizer is not None else self._realizer
        wait_for_sshd(
            hostname=vps_ip,
            port=effective_realizer.agent_endpoint(vps_ip).port,
            timeout_seconds=self.config.ssh_connect_timeout,
        )

    def _write_shutdown_script(self, host: Host, script_text: str) -> None:
        """Write ``script_text`` as the agent's executable ``commands/shutdown.sh``.

        The idle watcher runs ``commands/shutdown.sh``; only its contents vary by
        placement/provider (container PID-1 signal, VM poweroff, sentinel touch, or
        ARM deallocate), so the mkdir/write/chmod plumbing lives here once.
        """
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", script_text.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    def _create_shutdown_script(self, host: Host) -> None:
        """Create the shutdown script the idle watcher runs to stop the host.

        The action comes from the realizer: the container realizer signals the
        container's PID 1; the bare realizer powers the VM off directly. Cloud
        providers whose container path must stop the whole instance override this
        (sentinel + host-side watcher), early-returning here for the bare case.
        """
        self._write_shutdown_script(host, f"#!/bin/bash\n{self._realizer.idle_shutdown_command}\n")

    @abstractmethod
    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        """Parse build args, separating provisioning knobs from docker build args.

        Each concrete VpsProvider subclass implements its own parser
        because the set of accepted flags is per-provider (AWS has
        ``--aws-region=`` / ``--aws-instance-type=`` / ``--aws-ami=``; Vultr
        and OVH have a simpler region + plan shape; ``MinimalVpsProvider``
        accepts only ``--git-depth=`` and docker passthrough). For the common
        region + plan + git-depth shape, ``parse_vps_build_args`` is a ready-made
        convenience; for custom shapes, compose the lower-level helpers
        (``extract_single_value_arg``, ``extract_git_depth``,
        ``raise_if_vps_migration_arg``, ``raise_if_unknown_provider_arg``).
        """

    # =========================================================================
    # Core Lifecycle: stop_host
    # =========================================================================

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
        stop_reason: HostState | None = None,
    ) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        realizer = self._realizer_for_record(host_record)

        if create_snapshot:
            # Warn-not-raise: a stop preserves the volume, so the pre-stop snapshot is
            # a belt-and-suspenders extra -- a failed snapshot loses no data, and
            # blocking a requested stop over it would be worse. Mirrors the Modal and
            # Docker providers (mngr_modal/instance.py "Failed to create snapshot
            # before termination"; providers/docker/instance.py "Failed to create
            # snapshot before stop").
            try:
                self.create_snapshot(host_id)
            except MngrError as e:
                # FIXME: stopping a host should be like stopping an agent -- various components can fail, and those
                #  failures ought to be collected as the process continues, then at the very end, the exception can be
                #  raised (and include all of the things that went wrong, and triggrer a non-zero exit code, while
                #  still ensuring that we stop and clean up as much as possible)
                logger.warning("Failed to create snapshot before stop: {}", e)

        # Disconnect SSH before stopping (also disconnect the passed-in host
        # in case it is a different instance than the cached one).
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            realizer.stop_placement(outer, PlacementHandle.from_record(host_record), timeout_seconds)

            # Update the host record (bump updated_at). A subclass that stops more
            # than the container -- e.g. AWS stopping the EC2 instance -- passes a
            # ``stop_reason`` so the offline-state derivation reports it correctly
            # (STOPPED, not CRASHED) while the host is down; this single write
            # carries it, so the subclass needs no second write. The write must
            # land before any deeper stop, since the volume is unreachable after.
            host_store = realizer.open_host_store(outer, host_id)
            certified = host_record.certified_host_data
            data_updates = [to_update(certified.field_ref().updated_at, datetime.now(timezone.utc))]
            if stop_reason is not None:
                data_updates.append(to_update(certified.field_ref().stop_reason, stop_reason.value))
            updated_record = host_record.with_certified_updates(*data_updates)
            self._write_and_mirror(host_store, updated_record)

        self._host_record_cache[host_id] = updated_record
        logger.info("Host {} stopped", host_id)

    # =========================================================================
    # Core Lifecycle: start_host
    # =========================================================================

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        realizer = self._realizer_for_record(host_record)

        handle = PlacementHandle.from_record(host_record)
        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            realizer.start_placement(outer, handle)

            with log_span("Waiting for container SSH"):
                self._wait_for_container_sshd(host_record.vps_ip, realizer)

            host_obj = self._create_host_object(
                host_id, HostName(host_record.certified_host_data.host_name), host_record.vps_ip, realizer
            )

            # A resume is itself activity: refresh the BOOT activity file (whose
            # mtime is what the idle watcher reads) so the watcher relaunched just
            # below starts a fresh idle window. Without this, a resumed-but-idle
            # host keeps the pre-stop activity mtimes and the watcher re-stops it
            # within one poll -- so e.g. `mngr start` on an idle host would race a
            # near-immediate auto-stop. Must happen before the watcher relaunch.
            host_obj.record_activity(ActivitySource.BOOT)

            # The in-container activity watcher is a backgrounded process that does
            # not survive the container restart, so relaunch it -- else auto-stop-
            # on-idle would silently stop working after the first resume (for every
            # vps provider, not just AWS).
            with log_span("Relaunching activity watcher"):
                realizer.start_activity_watcher(outer, handle)

        logger.info("Host {} started", host_id)
        return host_obj

    # =========================================================================
    # Core Lifecycle: destroy_host
    # =========================================================================

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Destroy a VPS-backed host permanently.

        Best-effort: every teardown step is attempted. A failure that means a
        resource is already gone is benign (dropped). A failure that means a
        resource exists but could not be removed is real -- it is recorded as a
        ``CleanupFailure`` and collected, and the remaining steps still run. The
        collected failures are raised as a ``CleanupFailedGroup`` rather than
        aborting early or being silently swallowed. See
        specs/cleanup-error-aggregation.md.

        A missing host record still raises ``HostNotFoundError``; the
        orchestration layer classifies that abort.
        """
        host_id = host.id if isinstance(host, HostInterface) else host

        # Disconnect SSH before destroying (also disconnect the passed-in host
        # in case it is a different instance than the cached one).
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)
        realizer = self._realizer_for_record(host_record)

        vps_config = host_record.config
        vps_ip = host_record.vps_ip

        with collecting_cleanup_failures() as failures:
            if vps_ip is not None:
                with self._make_outer_for_vps_ip(vps_ip) as outer:
                    # Remove the agent placement and its per-host storage (container,
                    # btrfs subvolume, named volumes for the container realizer; a no-op
                    # for bare). The VPS-destroy below takes the whole loop file with it,
                    # so this is primarily belt-and-suspenders for a destroy retried on a
                    # still-existing VPS. The realizer records its own per-resource
                    # cleanup failures and raises a ``CleanupFailedGroup``, which we
                    # absorb into this destroy's aggregate.
                    try:
                        realizer.teardown_placement(outer, host_id, PlacementHandle.from_record(host_record))
                    except CleanupFailedGroup as group:
                        collect_cleanup_failures(failures, group)

            # Destroy the VPS instance. An "already gone" (HTTP 404/410) response is benign;
            # any other error means a VPS instance that may still exist (and incur cost).
            with log_span("Destroying VPS instance"):
                attempt_cloud_resource_teardown(
                    lambda: self.vps_client.destroy_instance(vps_config.vps_instance_id),
                    resource_description=f"VPS instance {vps_config.vps_instance_id}",
                    host_id=host_id,
                    failures=failures,
                )

            # Clean up SSH key from provider. An "already gone" (HTTP 404/410) response is
            # benign; any other error means a key that may still be registered.
            ssh_key_id = vps_config.vps_ssh_key_id
            if ssh_key_id is not None:
                attempt_cloud_resource_teardown(
                    lambda: self.vps_client.delete_ssh_key(ssh_key_id),
                    resource_description=f"SSH key {ssh_key_id}",
                    host_id=host_id,
                    failures=failures,
                )

            # Clean up local known_hosts. These are cosmetic local-file edits; a
            # missing file or OS error here leaves no infrastructure behind, so it is
            # always benign and never recorded as a failure.
            if vps_ip is not None:
                try:
                    remove_host_from_known_hosts(self._vps_known_hosts_path(), vps_ip, 22)
                except (OSError, UnicodeDecodeError) as e:
                    logger.trace("Failed to clean up VPS known_hosts: {}", e)
                try:
                    remove_host_from_known_hosts(
                        self._container_known_hosts_path(), vps_ip, self.config.container_ssh_port
                    )
                except (OSError, UnicodeDecodeError) as e:
                    logger.trace("Failed to clean up container known_hosts: {}", e)

            # Remove this host's unique on-disk host keypairs. Benign local cleanup
            # (the keys are useless once the host is gone); a missing dir or OS error
            # leaves no infrastructure behind, so it is never recorded as a failure.
            try:
                shutil.rmtree(per_host_key_dir(self._key_dir(), host_id), ignore_errors=True)
            except OSError as e:
                logger.trace("Failed to clean up per-host key dir: {}", e)

            self._delete_host_record_externally(host_id)
            logger.info("Host {} destroyed (VPS {})", host_id, vps_config.vps_instance_id)

    def delete_host(self, host: HostInterface) -> None:
        """Delete all local records for a destroyed host (does not destroy VPS)."""
        self._evict_cached_host(host.id)
        self._delete_host_record_externally(host.id)

    def on_connection_error(self, host_id: HostId) -> None:
        self._evict_cached_host(host_id)

    def outer_host_id_for(self, host_id: HostId) -> str | None:
        """Stable id for the outer (the VPS) of host_id, keyed by VPS IP."""
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)
        if host_record.vps_ip is None:
            return None
        return f"outer:{self.name}:{host_record.vps_ip}"

    @contextmanager
    def outer_host_for(self, host_id: HostId) -> Iterator[OuterHostInterface | None]:
        """Open the outer host (the VPS itself, root@vps_ip:22).

        Uses this provider's per-instance VPS SSH key (the one cloud-init
        injected on VPS provisioning).
        """
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            yield outer

    # =========================================================================
    # Discovery
    # =========================================================================

    def get_host(self, host: HostId | HostName) -> HostInterface:
        if isinstance(host, HostId) and host in self._host_by_id_cache:
            return self._host_by_id_cache[host]

        # Try to find via host records on all known VPSes
        # For now, we iterate all host records
        host_record = self._find_host_record(host)
        if host_record is None:
            raise HostNotFoundError(self.name, host)

        host_id = HostId(host_record.certified_host_data.host_id)
        vps_ip = host_record.vps_ip
        realizer = self._realizer_for_record(host_record)

        if vps_ip is not None and host_record.config is not None:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                # Check if the placement is running.
                if realizer.is_placement_running(outer, PlacementHandle.from_record(host_record)):
                    return self._create_host_object(
                        host_id, HostName(host_record.certified_host_data.host_name), vps_ip, realizer
                    )

        return self._create_offline_host(host_record)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)
        return self._create_offline_host(host_record)

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        """Discover all hosts managed by this provider."""
        discovered: list[DiscoveredHost] = []

        # Query all VPS instances from the provider API that have our tags
        # then SSH to each VPS to read host records from its unified host volume.

        # First, try to find any VPS instances for this provider
        # We'll need the host records from each VPS
        all_records = self._discover_host_records()

        for record in all_records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)
            discovered.append(
                DiscoveredHost(
                    host_id=host_id,
                    host_name=host_name,
                    provider_name=self.name,
                )
            )
            # Cache the host object
            if record.vps_ip is not None and record.config is not None:
                realizer = self._realizer_for_record(record)
                with self._make_outer_for_vps_ip(record.vps_ip) as outer:
                    if realizer.is_placement_running(outer, PlacementHandle.from_record(record)):
                        self._create_host_object(host_id, host_name, record.vps_ip, realizer)
                    else:
                        self._create_offline_host(record)
            else:
                self._create_offline_host(record)

        return discovered

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        """Load hosts and agent references from each VPS, reading agents live.

        For each VPS the realizer reads the host record and reads agent data
        **live** from the placement (container realizer: from the docker
        volume's bind-source path + the running container, so
        in-container-created agents are discovered; bare realizer: from the
        plain ``host_dir`` on the VM). The same live read reports the
        placement's running state, so no separate inspect round-trip is needed.
        """
        with log_span("VPS discover_hosts_and_agents for provider={}", self.name):
            discovery = self._discover_host_records_with_agents()
        agent_data_by_host_id = discovery.live_agent_data_by_host_id

        result: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
        for record in discovery.records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)

            # Cache the host record for later use by get_host_and_agent_details
            self._host_record_cache[host_id] = record

            # Running status came from the same live listing read. An entry in
            # is_running_by_host_id exists only when that read succeeded, which
            # requires the VPS to have been reachable -- so its presence doubles
            # as the reachability signal. A VPS that was unreachable during
            # discovery has no entry here, so it falls through to the
            # offline-state derivation rather than crashing the listing -- one
            # bad VPS must not drop the other VPSes' hosts.
            is_running = discovery.is_running_by_host_id.get(host_id, False)
            is_outer_reachable = host_id in discovery.is_running_by_host_id

            has_snapshots = len(record.certified_host_data.snapshots) > 0
            is_failed = record.certified_host_data.failure_reason is not None
            # A reachable VPS whose container is simply stopped (idle-watcher shutdown,
            # a manual `mngr stop`, or a VPS reboot) is cleanly STOPPED and restartable
            # via `mngr start` -- NOT crashed. The live listing read only succeeded
            # because the VPS is up, so this is never a host crash. Keep such hosts
            # visible to `mngr conn`/`start` (which pass include_destroyed=False)
            # instead of hiding them as if destroyed. An *unreachable* VPS with no
            # recorded stop_reason still derives to CRASHED below -- there the host
            # itself is down, which is the genuine failure case.
            is_cleanly_stopped = is_outer_reachable and not is_running

            if (
                not is_running
                and not is_cleanly_stopped
                and not is_failed
                and not has_snapshots
                and not include_destroyed
            ):
                continue

            if is_running and record.vps_ip is not None:
                host_state = HostState.RUNNING
                self._create_host_object(host_id, host_name, record.vps_ip, self._realizer_for_record(record))
            elif is_cleanly_stopped:
                host_state = HostState.STOPPED
                self._create_offline_host(record)
            else:
                host_state = derive_offline_host_state(
                    certified_data=record.certified_host_data,
                    supports_shutdown_hosts=self.supports_shutdown_hosts,
                    supports_snapshots=self.supports_snapshots,
                    has_snapshots=has_snapshots,
                )
                self._create_offline_host(record)

            host_ref = DiscoveredHost(
                host_id=host_id,
                host_name=host_name,
                provider_name=self.name,
                host_state=host_state,
            )

            # Build agent refs from live agent data
            agent_refs: list[DiscoveredAgent] = []
            for agent_data in agent_data_by_host_id.get(host_id, []):
                ref = validate_and_create_discovered_agent(agent_data, host_id, self.name)
                if ref is not None:
                    agent_refs.append(ref)

            result[host_ref] = agent_refs

        return result

    def _get_effective_auto_shutdown_seconds(self) -> int | None:
        """Return the auto-shutdown TTL (in seconds) to inject into cloud-init.

        Subclasses can override this to add provider-specific escape hatches
        (e.g., a test-only env-var that forces a TTL regardless of project
        config). The base implementation simply returns the configured value.
        """
        return self.config.auto_shutdown_seconds

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return SSH-reachable hostnames for VPSes owned by this provider instance.

        Each entry is whatever the provider hands back as the SSH target
        for one of its VPSes -- a public IPv4 (Vultr, AWS) or a provider DNS
        name (OVH classic VPS like ``vps-eec8860b.vps.ovh.us``). The
        discovery machinery below treats it as an opaque ``hostname``
        passed to paramiko.

        Concrete subclasses implement this by querying their provider's
        listing API (e.g. by tag) and resolving the matching instances'
        SSH targets. The remaining discovery machinery -- parallel SSH
        into each VPS, reading host records and agent data from each
        VPS's unified host volume, caching -- is shared and lives in
        this base class.

        Default returns ``[]`` so test doubles and providers without a
        listing API can opt out without overriding.
        """
        return []

    def _credentials_configured(self) -> bool:
        """Return True iff the provider's API credentials are resolvable.

        Used by ``_find_host_record`` to short-circuit a full discovery sweep
        when no credentials are available. Subclasses override to check their
        own credential source. The default returns True so providers that
        don't carry credentials (test doubles, OVH IAM via env) opt in
        automatically.
        """
        return True

    def _read_records_from_vps(
        self,
        vps_ip: str,
    ) -> _VpsDiscoveryData:
        """Read the (single) host record + live agent data from one VPS.

        Each VPS hosts exactly one mngr placement (1:1 invariant). The realizer
        locates and reads the host record (container realizer: finds the
        container by its host-id label, derives its unified volume name, and
        reads host_state.json from the volume's bind-source path -- the per-host
        btrfs subvolume the volume's ``Options.device`` points at; bare
        realizer: reads the record straight from the fixed store path).

        Agent data is read **live** from the placement's ``host_dir`` -- not
        from the persisted ``agents/*.json`` outer store. The outer store is
        only written by the host-side mngr at agent-create time, so it misses
        agents created *inside* the placement (e.g. by an in-placement ``mngr
        create``); reading live ensures those agents are discoverable by ``mngr
        message`` and friends. The same call reports the placement's running
        state, avoiding a separate inspect.

        If the placement does not exist yet (e.g., the VPS is still being set
        up by a concurrent ``mngr create``), returns empty results. If outer
        SSH to the VPS fails, fall back to any in-process cached records for
        that VPS so the hosts still appear in the listing (with an offline
        state) instead of disappearing entirely; one bad VPS must not silently
        drop its hosts.
        """
        # Probe with the realizer matching THIS host's placement (resolved from the
        # instance's ``mngr-isolation`` marker, no SSH needed), not the provider's
        # create-time default -- otherwise a bare host probed by the default
        # container realizer finds no container and is invisible to discovery.
        # Resolved inside the try so a corrupt marker (a VpsError) degrades just
        # this VPS rather than aborting the whole discovery sweep.
        try:
            realizer = self._realizer_for_vps_ip(vps_ip)
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                found = realizer.find_host_record(outer)
                if found is None:
                    logger.debug("No mngr host on VPS {} yet, skipping", vps_ip)
                    return _VpsDiscoveryData()
                host_id, record = found
                # The host record read above succeeded, so the host exists and
                # must appear in the listing. A failure of the *live-listing*
                # read alone (e.g. a script racing a placement restart) must not
                # drop the host -- degrade to no live agents and an offline
                # (not-running) state instead. Genuine VPS-unreachable failures
                # fail earlier (in find_host_record) and are handled by the outer
                # cache-fallback branch.
                try:
                    live_agent_data, is_running = realizer.read_live_listing(
                        outer,
                        host_id,
                        str(self.host_dir),
                        self.mngr_ctx.config.prefix,
                        self.mngr_ctx.config.tmux.primary_window_name,
                    )
                except MngrError as listing_exc:
                    logger.warning(
                        "Live listing read failed for host {} on VPS {}; surfacing host as offline: {}",
                        host_id,
                        vps_ip,
                        listing_exc,
                    )
                    return _VpsDiscoveryData(records=(record,))
                return _VpsDiscoveryData(
                    records=(record,),
                    live_agent_data_by_host_id={host_id: live_agent_data},
                    is_running_by_host_id={host_id: is_running},
                )
        except MngrError as e:
            cached_records = [r for r in self._host_record_cache.values() if r.vps_ip == vps_ip]
            if cached_records:
                logger.warning(
                    "Failed to read records from VPS {} ({}); surfacing {} cached host record(s) as offline",
                    vps_ip,
                    e,
                    len(cached_records),
                )
            else:
                logger.warning("Failed to read records from VPS {}: {}", vps_ip, e)
            return _VpsDiscoveryData(records=tuple(cached_records))

    def _discover_host_records_with_agents(self) -> _VpsDiscoveryData:
        """Discover host records and live agent data from all VPSes for this provider.

        Calls ``_list_provider_vps_hostnames`` to enumerate VPSes
        (provider-specific), then SSHes to each in parallel. Within each
        VPS, ``_read_records_from_vps`` finds the mngr container by host-id
        label, reads ``host_state.json``, and reads live agent data from the
        container; the parallel fan-out across VPSes keeps wall time bounded
        by the slowest VPS rather than the sum.
        """
        vps_ips = self._list_provider_vps_hostnames()
        if not vps_ips:
            return _VpsDiscoveryData()

        all_records: list[VpsHostRecord] = []
        all_agent_data: dict[HostId, list[dict[str, Any]]] = {}
        all_running: dict[HostId, bool] = {}

        cg_name = f"{type(self).__name__}-discover"
        with log_span("Reading records from {} VPS instance(s) in parallel", len(vps_ips)):
            cg = ConcurrencyGroup(name=cg_name)
            with cg:
                with ConcurrencyGroupExecutor(
                    parent_cg=cg,
                    name=f"{cg_name}_read_records",
                    max_workers=min(len(vps_ips), 32),
                ) as executor:
                    futures = [executor.submit(self._read_records_from_vps, ip) for ip in vps_ips]

                for future in futures:
                    vps_data = future.result()
                    all_records.extend(vps_data.records)
                    for host_id, agents in vps_data.live_agent_data_by_host_id.items():
                        all_agent_data.setdefault(host_id, []).extend(agents)
                    all_running.update(vps_data.is_running_by_host_id)

        return _VpsDiscoveryData(
            records=tuple(all_records),
            live_agent_data_by_host_id=all_agent_data,
            is_running_by_host_id=all_running,
        )

    def _discover_host_records(self) -> list[VpsHostRecord]:
        """Discover host records by enumerating this provider's VPSes."""
        return list(self._discover_host_records_with_agents().records)

    def _find_host_record(self, host: HostId | HostName) -> VpsHostRecord | None:
        """Find a host record by ID or name, using cache first."""
        if isinstance(host, HostId) and host in self._host_record_cache:
            return self._host_record_cache[host]
        if isinstance(host, HostName):
            for cached_record in self._host_record_cache.values():
                if cached_record.certified_host_data.host_name == str(host):
                    return cached_record

        if not self._credentials_configured():
            logger.warning("{} credentials not configured, cannot resolve host", self.config.backend)
            return None

        records = self._discover_host_records()
        for record in records:
            host_id = HostId(record.certified_host_data.host_id)
            self._host_record_cache[host_id] = record
            if isinstance(host, HostId) and record.certified_host_data.host_id == str(host):
                return record
            elif isinstance(host, HostName) and record.certified_host_data.host_name == str(host):
                return record
        return None

    # =========================================================================
    # Optimized Listing
    # =========================================================================

    def get_host_and_agent_details(
        self,
        host_ref: DiscoveredHost,
        agent_refs: Sequence[DiscoveredAgent],
        field_generators: Mapping[str, Mapping[str, Callable[[AgentInterface, OnlineHostInterface], Any]]]
        | None = None,
        offline_field_generators: Mapping[str, Mapping[str, Callable[[DiscoveredAgent, HostDetails], Any]]]
        | None = None,
        on_error: Callable[[DiscoveredAgent | DiscoveredHost, BaseException], None] | None = None,
    ) -> tuple[HostDetails, list[AgentDetails]]:
        """Build HostDetails and AgentDetails via a single SSH command."""
        # Look up cached host record (populated during discover_hosts_and_agents)
        host_record = self._host_record_cache.get(host_ref.host_id)
        if host_record is None:
            host_record = self._find_host_record(host_ref.host_id)

        # For offline hosts or hosts without a record, fall back to default
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            return super().get_host_and_agent_details(
                host_ref,
                agent_refs,
                field_generators=field_generators,
                offline_field_generators=offline_field_generators,
                on_error=on_error,
            )

        try:
            host = self.get_host(host_ref.host_id)

            if not isinstance(host, Host):
                return super().get_host_and_agent_details(
                    host_ref,
                    agent_refs,
                    field_generators=field_generators,
                    offline_field_generators=offline_field_generators,
                    on_error=on_error,
                )

            # Collect all data in one SSH command
            script = build_listing_collection_script(str(self.host_dir), self.mngr_ctx.config.prefix)

            with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
                with log_span("Collecting listing data via single SSH command"):
                    raw_output = self._realizer_for_record(host_record).collect_listing_output(
                        outer,
                        PlacementHandle.from_record(host_record),
                        script,
                        timeout_seconds=30.0,
                    )

            raw = parse_listing_collection_output(raw_output)

        except HostConnectionError as e:
            self.on_connection_error(host_ref.host_id)
            logger.debug(
                "Host {} unreachable during optimized listing, falling back to default: {}",
                host_ref.host_id,
                e,
            )
            return super().get_host_and_agent_details(
                host_ref,
                agent_refs,
                field_generators=field_generators,
                offline_field_generators=offline_field_generators,
                on_error=on_error,
            )
        except MngrError as e:
            if on_error:
                on_error(host_ref, e)
                return HostDetails(
                    id=host_ref.host_id,
                    name=str(host_ref.host_name),
                    provider_name=host_ref.provider_name,
                    state=HostState.RUNNING,
                ), []
            else:
                raise

        host_details = self._build_host_details_from_raw(host, host_ref, host_record, raw)
        agent_details_list = self._build_agent_details_from_raw(host_details, host_record.certified_host_data, raw)
        return host_details, agent_details_list

    def _build_host_details_from_raw(
        self,
        host: Host,
        host_ref: DiscoveredHost,
        host_record: VpsHostRecord,
        raw: dict[str, Any],
    ) -> HostDetails:
        """Construct HostDetails from cached host record and SSH-collected data."""
        ssh_info: SSHInfo | None = None
        ssh_connection = host.get_ssh_connection_info()
        if ssh_connection is not None:
            user, hostname, port, key_path = ssh_connection
            ssh_info = SSHInfo(
                user=user,
                host=hostname,
                port=port,
                key_path=key_path,
                command=f"ssh -i {key_path} -p {port} {user}@{hostname}",
            )

        boot_time = timestamp_to_datetime(raw.get("btime"))
        uptime_seconds = raw.get("uptime_seconds")
        resource = self.get_host_resources(host)

        lock_mtime = raw.get("lock_mtime")
        # The lock file persists after release (its inode must stay stable across
        # local and remote holders), so its mtime alone does not indicate "held".
        # Use the real flock held-probe collected by the listing script.
        is_locked = bool(raw.get("is_lock_held"))
        locked_time = (
            datetime.fromtimestamp(lock_mtime, tz=timezone.utc) if is_locked and lock_mtime is not None else None
        )

        certified_data: CertifiedHostData | None = None
        certified_data_dict = raw.get("certified_data")
        if certified_data_dict is not None:
            try:
                certified_data = CertifiedHostData.model_validate(certified_data_dict)
            except (ValueError, KeyError) as e:
                logger.warning("Failed to validate host data.json from SSH output: {}", e)
        if certified_data is None:
            certified_data = host_record.certified_host_data

        tags = dict(certified_data.user_tags)

        ssh_activity_mtime = raw.get("ssh_activity_mtime")
        ssh_activity = (
            datetime.fromtimestamp(ssh_activity_mtime, tz=timezone.utc) if ssh_activity_mtime is not None else None
        )

        snapshots = self.list_snapshots(host)

        return HostDetails(
            id=host.id,
            name=certified_data.host_name,
            provider_name=host_ref.provider_name,
            state=HostState.RUNNING,
            image=certified_data.image,
            tags=tags,
            boot_time=boot_time,
            uptime_seconds=uptime_seconds,
            resource=resource,
            ssh=ssh_info,
            snapshots=snapshots,
            is_locked=is_locked,
            locked_time=locked_time,
            plugin=certified_data.plugin,
            ssh_activity_time=ssh_activity,
            failure_reason=certified_data.failure_reason,
        )

    def _build_agent_details_from_raw(
        self,
        host_details: HostDetails,
        certified_host_data: CertifiedHostData,
        raw: dict[str, Any],
    ) -> list[AgentDetails]:
        """Build AgentDetails objects from SSH-collected agent data."""
        idle_timeout_seconds = certified_host_data.idle_timeout_seconds
        activity_sources = certified_host_data.activity_sources
        idle_mode = certified_host_data.idle_mode

        ssh_activity = timestamp_to_datetime(raw.get("ssh_activity_mtime"))
        ps_output = raw.get("ps_output", "")

        agent_details_list: list[AgentDetails] = []
        for agent_raw in raw.get("agents", []):
            try:
                agent_details = self._build_single_agent_details(
                    agent_raw=agent_raw,
                    host_details=host_details,
                    ssh_activity=ssh_activity,
                    ps_output=ps_output,
                    idle_timeout_seconds=idle_timeout_seconds,
                    activity_sources=activity_sources,
                    idle_mode=idle_mode,
                )
                if agent_details is not None:
                    agent_details_list.append(agent_details)
            except (ValueError, KeyError, TypeError) as e:
                agent_id = agent_raw.get("data", {}).get("id", "unknown")
                logger.warning("Failed to build listing info for agent {}: {}", agent_id, e)

        return agent_details_list

    def _build_single_agent_details(
        self,
        agent_raw: dict[str, Any],
        host_details: HostDetails,
        ssh_activity: datetime | None,
        ps_output: str,
        idle_timeout_seconds: int,
        activity_sources: tuple[ActivitySource, ...],
        idle_mode: IdleMode,
    ) -> AgentDetails | None:
        """Build a single AgentDetails from raw SSH-collected data."""
        agent_data = agent_raw.get("data", {})
        agent_id_str = agent_data.get("id")
        agent_name_str = agent_data.get("name")
        if not agent_id_str or not agent_name_str:
            logger.warning("Skipped agent with missing id or name in listing data: {}", agent_data)
            return None

        agent_type = str(agent_data.get("type", "unknown"))
        command = CommandString(agent_data.get("command", "bash"))
        create_time_str = agent_data.get("create_time")
        try:
            create_time = (
                datetime.fromisoformat(create_time_str)
                if create_time_str
                else datetime(1970, 1, 1, tzinfo=timezone.utc)
            )
        except (ValueError, TypeError) as e:
            logger.warning("Failed to parse create_time for agent {}: {}", agent_id_str, e)
            create_time = datetime(1970, 1, 1, tzinfo=timezone.utc)

        user_activity = timestamp_to_datetime(agent_raw.get("user_activity_mtime"))
        agent_activity = timestamp_to_datetime(agent_raw.get("agent_activity_mtime"))
        start_time = timestamp_to_datetime(agent_raw.get("start_activity_mtime"))
        now = datetime.now(timezone.utc)
        runtime_seconds = (now - start_time).total_seconds() if start_time else None
        idle_seconds = compute_idle_seconds(user_activity, agent_activity, ssh_activity)

        expected_process_name = resolve_expected_process_name(agent_type, command, self.mngr_ctx.config)
        is_type_known = check_agent_type_known(agent_type, self.mngr_ctx.config)
        state = determine_lifecycle_state(
            tmux_info=agent_raw.get("tmux_info"),
            is_active=agent_raw.get("is_active", False),
            expected_process_name=expected_process_name,
            ps_output=ps_output,
            is_agent_type_known=is_type_known,
        )

        return AgentDetails(
            id=AgentId(agent_id_str),
            name=AgentName(agent_name_str),
            type=agent_type,
            command=command,
            work_dir=Path(agent_data.get("work_dir", "/")),
            initial_branch=agent_data.get("created_branch_name"),
            create_time=create_time,
            start_on_boot=agent_data.get("start_on_boot", False),
            state=state,
            url=agent_raw.get("url"),
            start_time=start_time,
            runtime_seconds=runtime_seconds,
            user_activity_time=user_activity,
            agent_activity_time=agent_activity,
            idle_seconds=idle_seconds,
            idle_mode=idle_mode.value,
            idle_timeout_seconds=idle_timeout_seconds,
            activity_sources=tuple(s.value for s in activity_sources),
            labels=agent_data.get("labels", {}),
            host=host_details,
            plugin={},
        )

    # =========================================================================
    # Snapshots
    # =========================================================================

    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        # Gate on the provider-level capability up front (before any host lookup),
        # so a snapshot-incapable provider fails cleanly; the per-host realizer is
        # then re-checked once the record is known (a bare host on a snapshot-capable
        # provider still has no snapshots).
        self._require_snapshot_capable_realizer(self._realizer)
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        host_realizer = self._realizer_for_record(host_record)
        realizer = self._require_snapshot_capable_realizer(host_realizer)

        snapshot_name = name or SnapshotName(f"mngr-snapshot-{host_id}-{int(time.time())}")

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            snapshot_id = realizer.snapshot_placement(outer, host_id, PlacementHandle.from_record(host_record))

            # Store snapshot record in host data
            snapshot_record = SnapshotRecord(
                id=str(snapshot_id),
                name=str(snapshot_name),
                created_at=datetime.now(timezone.utc).isoformat(),
            )

            # Update certified data with new snapshot
            existing_snapshots = host_record.certified_host_data.snapshots
            updated_snapshots = list(existing_snapshots) + [snapshot_record]
            certified = host_record.certified_host_data
            updated_record = host_record.with_certified_updates(
                to_update(certified.field_ref().snapshots, updated_snapshots),
                to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
            )

            # ``host_record.config`` is guaranteed non-None by the guard at the top of this method.
            host_store = host_realizer.open_host_store(outer, host_id)
            self._write_and_mirror(host_store, updated_record)

        logger.info("Created snapshot {} for host {}", snapshot_name, host_id)
        return snapshot_id

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)

        snapshots = host_record.certified_host_data.snapshots
        return [
            SnapshotInfo(
                id=SnapshotId(s.id),
                name=SnapshotName(s.name),
                created_at=datetime.fromisoformat(s.created_at),
            )
            for s in snapshots
        ]

    def delete_snapshot(self, host: HostInterface | HostId, snapshot_id: SnapshotId) -> None:
        # Provider-level gate up front (matches create_snapshot), then narrow to the
        # host's own placement realizer once the record is resolved.
        self._require_snapshot_capable_realizer(self._realizer)
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        realizer = self._require_snapshot_capable_realizer(self._realizer_for_record(host_record))

        certified = host_record.certified_host_data
        remaining_snapshots = [s for s in certified.snapshots if s.id != str(snapshot_id)]
        if len(remaining_snapshots) == len(certified.snapshots):
            raise SnapshotNotFoundError(self.name, snapshot_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            # Delete the image first: on a real failure this raises (the snapshot stays
            # in the record, so it still lists), and only on success do we drop it from
            # the record -- so the record never claims a snapshot is gone while its image
            # remains. (create_snapshot is the inverse: image, then record.)
            realizer.delete_snapshot_placement(outer, snapshot_id)
            updated_record = host_record.with_certified_updates(
                to_update(certified.field_ref().snapshots, remaining_snapshots),
                to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            host_store = realizer.open_host_store(outer, host_id)
            self._write_and_mirror(host_store, updated_record)

        # Refresh the cache so a same-process ``list_snapshots`` (cache-first) does not
        # still report the just-deleted snapshot.
        self._host_record_cache[host_id] = updated_record
        logger.info("Deleted snapshot {} for host {}", snapshot_id, host_id)

    # =========================================================================
    # Tags
    # =========================================================================

    def get_host_tags(self, host: HostInterface | HostId) -> dict[str, str]:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)
        return dict(host_record.certified_host_data.user_tags)

    def set_host_tags(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("VPS provider does not support mutable tags")

    def add_tags_to_host(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("VPS provider does not support mutable tags")

    def remove_tags_from_host(self, host: HostInterface | HostId, keys: Sequence[str]) -> None:
        raise MngrError("VPS provider does not support mutable tags")

    def rename_host(self, host: HostInterface | HostId, name: HostName) -> HostInterface:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)

        certified = host_record.certified_host_data
        updated_record = host_record.with_certified_updates(
            to_update(certified.field_ref().host_name, str(name)),
            to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
        )

        if host_record.vps_ip is not None:
            if host_record.config is None:
                raise MngrError(
                    f"Host record for {host_id} on VPS {host_record.vps_ip} is missing config -- "
                    "cannot determine unified volume name to rename"
                )
            with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
                host_store = self._realizer_for_record(host_record).open_host_store(outer, host_id)
                self._write_and_mirror(host_store, updated_record)

        # Re-stamp the cheap host-name identity tag/metadata that offline discovery
        # reads (it never reads the mirrored record), so a renamed host no longer
        # resolves under its old name once stopped. Runs via the cloud API, so it
        # works whether the host is up or stopped; default no-op for providers with
        # no such identity tag.
        self._remirror_host_name(updated_record, name)

        return self.get_host(host_id)

    def _remirror_host_name(self, host_record: VpsHostRecord, name: HostName) -> None:
        """Re-stamp the create-time host-name identity tag/metadata after a rename.

        Offline discovery recovers a stopped host's name from a cheap instance
        tag/metadata stamped at create (the EC2 ``Name`` tag, the Azure/GCP
        ``mngr-host-name`` tag/metadata) -- not from the mirrored record -- so without
        this a renamed host still lists under its old name once stopped. The value
        matches create's ``label`` (``mngr-<host_name>``). Default no-op (a provider
        with no such identity tag needs nothing); offline-capable cloud providers
        override to update it through their cloud API.
        """
        del host_record, name

    # =========================================================================
    # Volumes
    # =========================================================================

    def list_volumes(self) -> list[VolumeInfo]:
        return []

    def delete_volume(self, volume_id: VolumeId) -> None:
        pass

    # =========================================================================
    # Resources
    # =========================================================================

    def get_host_resources(self, host: HostInterface) -> HostResources:
        return HostResources(
            cpu=CpuResources(count=1, frequency_ghz=None),
            memory_gb=1.0,
            disk_gb=None,
            gpu=None,
        )

    # =========================================================================
    # Connector
    # =========================================================================

    def get_connector(self, host: HostInterface | HostId) -> PyinfraHost:
        resolved = self.get_host(host.id if isinstance(host, HostInterface) else host)
        if isinstance(resolved, Host):
            return resolved.connector.host
        raise MngrError("Cannot get connector for offline host")

    # =========================================================================
    # Agent Data Persistence
    # =========================================================================

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = self._realizer_for_record(host_record).open_host_store(outer, host_id)
            records = host_store.list_persisted_agent_data()
        logger.debug("Read {} persisted agent record(s) for host {}", len(records), host_id)
        return records

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = self._realizer_for_record(host_record).open_host_store(outer, host_id)
            host_store.persist_agent_data(agent_data)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = self._realizer_for_record(host_record).open_host_store(outer, host_id)
            host_store.remove_persisted_agent_data(agent_id)


class MinimalVpsProvider(VpsProvider):
    """``VpsProvider`` for use cases where VPS provisioning is externally managed.

    Pairs with a ``vps_client`` whose provisioning calls raise (e.g.
    ``ExternallyManagedVpsClient``): some other system (an imbue_cloud pool
    lease, a hand-rolled provisioner, etc.) already created the VPS. This
    provider only ever runs the post-provisioning host-setup machinery --
    ``teardown_container_on_existing_vps`` and ``create_host_on_existing_vps``
    -- which take a caller-supplied ``outer`` and make no cloud-API calls.

    Build args here are just the docker-side knobs that flow through to
    ``docker build`` on the leased VPS: there is no cloud provider to
    select a region / plan / image for. The parser extracts ``--git-depth=N``
    (still relevant -- it controls the *local* mngr build context, not the
    VPS) and forwards everything else verbatim. The legacy shared
    ``--vps-*`` prefix is rejected with a migration error so a caller that
    still passes the old shape gets a clear pointer rather than having the
    arg silently land in docker.
    """

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        # Composed from the shared helpers rather than hand-rolling the same
        # git-depth / --vps-* migration handling: this is the same pattern
        # ``parse_vps_build_args`` and the AWS provider use, just without any
        # region / plan extraction (provisioning lives outside this provider).
        args = list(build_args or ())
        git_depth, args = extract_git_depth(args)
        docker_build_args: list[str] = []
        for arg in args:
            raise_if_vps_migration_arg(arg)
            docker_build_args.append(arg)
        # ``region`` / ``plan`` are unused on the externally-managed path
        # (callers pass them as explicit kwargs to ``create_host_on_existing_vps``),
        # so the sentinels are harmless.
        return ParsedVpsBuildOptions(
            region="",
            plan="",
            git_depth=git_depth,
            docker_build_args=tuple(docker_build_args),
        )
