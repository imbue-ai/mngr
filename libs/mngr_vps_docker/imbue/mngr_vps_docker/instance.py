import json
import os
import shlex
import time
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import compute_idle_seconds
from imbue.mngr.hosts.common import determine_lifecycle_state
from imbue.mngr.hosts.common import resolve_expected_process_name
from imbue.mngr.hosts.common import timestamp_to_datetime
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import derive_offline_host_state
from imbue.mngr.hosts.offline_host import validate_and_create_discovered_agent
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CertifiedHostData
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
from imbue.mngr.primitives import build_user_ssh_command
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.listing_utils import build_listing_collection_script
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_vps_docker.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig
from imbue.mngr_vps_docker.container_setup import CONTAINER_ENTRYPOINT_CMD
from imbue.mngr_vps_docker.container_setup import HOST_DIR_SUBPATH
from imbue.mngr_vps_docker.container_setup import HOST_VOLUME_MOUNT_PATH
from imbue.mngr_vps_docker.container_setup import LABEL_HOST_ID
from imbue.mngr_vps_docker.container_setup import LABEL_HOST_NAME
from imbue.mngr_vps_docker.container_setup import LABEL_PROVIDER
from imbue.mngr_vps_docker.container_setup import LABEL_TAGS
from imbue.mngr_vps_docker.container_setup import SNAPSHOT_READ_MOUNT_PATH
from imbue.mngr_vps_docker.container_setup import SNAPSHOT_TRIGGER_MOUNT_PATH
from imbue.mngr_vps_docker.container_setup import build_image_on_outer_from_build_args
from imbue.mngr_vps_docker.container_setup import check_file_exists_on_outer
from imbue.mngr_vps_docker.container_setup import commit_container
from imbue.mngr_vps_docker.container_setup import create_bind_volume_on_outer
from imbue.mngr_vps_docker.container_setup import delete_btrfs_subvolume_on_outer
from imbue.mngr_vps_docker.container_setup import docker_inspect_running
from imbue.mngr_vps_docker.container_setup import exec_in_container
from imbue.mngr_vps_docker.container_setup import host_volume_name_for
from imbue.mngr_vps_docker.container_setup import prepare_btrfs_on_outer
from imbue.mngr_vps_docker.container_setup import provision_snapshot_helper_on_outer
from imbue.mngr_vps_docker.container_setup import pull_image
from imbue.mngr_vps_docker.container_setup import remove_container
from imbue.mngr_vps_docker.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps_docker.container_setup import remove_volume
from imbue.mngr_vps_docker.container_setup import run_container
from imbue.mngr_vps_docker.container_setup import run_docker
from imbue.mngr_vps_docker.container_setup import seed_host_volume_layout_on_outer
from imbue.mngr_vps_docker.container_setup import setup_container_ssh
from imbue.mngr_vps_docker.container_setup import snapshot_trigger_volume_name_for
from imbue.mngr_vps_docker.container_setup import start_container
from imbue.mngr_vps_docker.container_setup import stop_container
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import VpsHostConfig
from imbue.mngr_vps_docker.host_store import open_host_store
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface


class ParsedVpsBuildOptions(FrozenModel):
    """Result of parsing VPS-specific build args from Docker build args."""

    region: str = Field(description="VPS region")
    plan: str = Field(description="VPS plan")
    os_id: int | str = Field(
        description=(
            "VPS OS image identifier. Most providers use an integer image id; "
            "providers that catalog images by name (e.g. OVH classic VPS) use a string."
        ),
    )
    git_depth: int | None = Field(
        default=None, description="Git clone depth for build context, or None for full clone"
    )
    docker_build_args: tuple[str, ...] = Field(description="Remaining args passed to docker build")


def _parse_build_args(
    build_args: Sequence[str] | None,
    *,
    default_region: str,
    default_plan: str,
    default_os_id: int | str,
) -> ParsedVpsBuildOptions:
    """Parse build args, separating VPS provisioning args from Docker build args.

    VPS-specific args use the --vps- prefix (e.g., --vps-region=ewr).
    ``--git-depth=N`` controls the git clone depth for the build context.
    Everything else is passed through to docker build on the VPS.

    ``--vps-os=`` values are cast to ``int`` only when ``default_os_id`` is
    itself an integer (preserving Vultr's existing semantics). When the
    default is a string the parsed value is kept as a string -- providers
    like OVH classic VPS use string image names rather than integer ids.
    """
    region = default_region
    plan = default_plan
    os_id: int | str = default_os_id
    git_depth: int | None = None
    docker_build_args: list[str] = []

    if build_args:
        for arg in build_args:
            if arg.startswith("--vps-region="):
                region = arg.split("=", 1)[1]
            elif arg.startswith("--vps-plan="):
                plan = arg.split("=", 1)[1]
            elif arg.startswith("--vps-os="):
                raw_os = arg.split("=", 1)[1]
                os_id = int(raw_os) if isinstance(default_os_id, int) else raw_os
            elif arg.startswith("--git-depth="):
                git_depth = int(arg.split("=", 1)[1])
            elif arg.startswith("--vps-"):
                raise MngrError(
                    f"Unknown VPS build arg: {arg}. Valid VPS args: --vps-region=, --vps-plan=, --vps-os=, --git-depth="
                )
            else:
                docker_build_args.append(arg)

    return ParsedVpsBuildOptions(
        region=region,
        plan=plan,
        os_id=os_id,
        git_depth=git_depth,
        docker_build_args=tuple(docker_build_args),
    )


def _read_host_id_label_from_vps(outer: OuterHostInterface) -> HostId | None:
    """Return the host_id label of the (single) mngr container on this VPS, if any.

    Each VPS hosts at most one mngr container (1:1 invariant), so the value
    of the ``com.imbue.mngr.host-id`` label on any container with that label
    set uniquely identifies the VPS's host. Returns ``None`` when no such
    container exists yet (e.g., the VPS is still being provisioned).

    Includes stopped containers so a paused host is still discoverable.
    """
    fmt = "{{index .Config.Labels " + json.dumps(LABEL_HOST_ID) + "}}"
    result = outer.execute_idempotent_command(
        "docker ps -a -q "
        f"--filter {shlex.quote('label=' + LABEL_HOST_ID)} | "
        f"xargs -r docker inspect --format {shlex.quote(fmt)}",
    )
    if not result.success:
        raise MngrError(
            f"Failed to list mngr containers on VPS: stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
    for raw_line in result.stdout.splitlines():
        value = raw_line.strip()
        if not value:
            continue
        try:
            return HostId(value)
        except InvalidRandomIdError as e:
            # A corrupted/manually-edited label must not crash discovery for
            # the whole VPS; surface as MngrError so the existing fallback
            # path in _read_records_from_vps logs and continues.
            raise MngrError(f"Container on VPS has malformed {LABEL_HOST_ID} label {value!r}: {e}") from e
    return None


def build_vps_tags(host_id: HostId, provider_name: str, extra_tags_raw: str) -> list[str]:
    """Compose the tag list passed to the VPS create call.

    Always emits ``mngr-host-id=<id>`` and ``mngr-provider=<name>``. The
    ``extra_tags_raw`` string is a comma-separated list of ``key=value``
    tags that the spawning caller wants attached at create time -- e.g.
    minds-side pool-bake sets ``MNGR_VPS_EXTRA_TAGS=minds_env=<name>``
    so the env's destroy can later find + delete the instance via the
    Vultr tag filter. Empty / whitespace-only entries are skipped so
    trailing commas don't produce blank tags.

    Pulled out to module scope so the comma-splitting behaviour is unit
    testable without standing up an entire provisioning flow.
    """
    tags = [f"mngr-host-id={host_id}", f"mngr-provider={provider_name}"]
    for tag in extra_tags_raw.split(","):
        stripped = tag.strip()
        if stripped:
            tags.append(stripped)
    return tags


class VpsDockerProvider(BaseProviderInstance):
    """Provider that runs agents in Docker containers on VPS instances.

    Each host maps to exactly one VPS running exactly one Docker container.
    The VPS stays running at all times; stop/start operates on the container.
    Destroying the host destroys both the container and the VPS.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: VpsDockerProviderConfig = Field(frozen=True, description="VPS Docker provider configuration")
    vps_client: VpsClientInterface = Field(frozen=True, description="VPS provider API client")

    _host_record_cache: dict[HostId, VpsDockerHostRecord] = PrivateAttr(default_factory=dict)
    _container_running_cache: dict[str, bool] = PrivateAttr(default_factory=dict)

    @property
    def supports_snapshots(self) -> bool:
        return True

    @property
    def supports_shutdown_hosts(self) -> bool:
        return True

    @property
    def supports_volumes(self) -> bool:
        return True

    @property
    def supports_mutable_tags(self) -> bool:
        return False

    def reset_caches(self) -> None:
        for host_id in list(self._host_by_id_cache):
            self._evict_cached_host(host_id)
        self._host_record_cache.clear()
        self._container_running_cache.clear()

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
        return load_or_create_ssh_keypair(self._key_dir(), "vps_ssh_key")

    def _get_container_ssh_keypair(self) -> tuple[Path, str]:
        """Load or create the SSH keypair for authenticating to the container."""
        return load_or_create_ssh_keypair(self._key_dir(), "container_ssh_key")

    def _get_vps_host_keypair(self) -> tuple[Path, str]:
        """Load or create the Ed25519 host keypair injected into VPS via cloud-init."""
        return load_or_create_host_keypair(self._key_dir(), "host_key")

    def _get_container_host_keypair(self) -> tuple[Path, str]:
        """Load or create the Ed25519 host keypair for the container's sshd."""
        return load_or_create_host_keypair(self._key_dir(), "container_host_key")

    def _vps_known_hosts_path(self) -> Path:
        return self._key_dir() / "vps_known_hosts"

    def _container_known_hosts_path(self) -> Path:
        return self._key_dir() / "container_known_hosts"

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
    # The store is opened via ``open_host_store(outer, volume_name)`` (free
    # function in ``host_store``) which resolves the volume's bind-source path
    # via ``docker volume inspect --format '{{.Options.device}}'`` -- the docker
    # named volume is a bind-options entry pointing at the per-host btrfs
    # subvolume, so ``Options.device`` (not the unused ``Mountpoint``
    # placeholder under ``/var/lib/docker/volumes``) is the real on-disk path.
    # This used to be the per-user state container.

    # =========================================================================
    # Host Object Construction
    # =========================================================================

    def _create_host_object(
        self,
        host_id: HostId,
        host_name: HostName,
        vps_ip: str,
    ) -> Host:
        """Create a Host object with direct SSH to the container via the VPS's exposed port."""
        container_key_path, _container_pub = self._get_container_ssh_keypair()

        # Container sshd port is exposed on the VPS's public IP.
        # We connect directly to vps_ip:container_ssh_port.
        pyinfra_host = create_pyinfra_host(
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            private_key_path=container_key_path,
            known_hosts_path=self._container_known_hosts_path(),
        )

        connector = PyinfraConnector(pyinfra_host)
        host = Host(
            id=host_id,
            host_name=host_name,
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data, vps_ip
            ),
        )
        self._evict_cached_host(host_id, replacement=host)
        return host

    def _create_offline_host(
        self,
        host_record: VpsDockerHostRecord,
    ) -> OfflineHost:
        """Create an OfflineHost from a host record."""
        host_id = HostId(host_record.certified_host_data.host_id)
        vps_ip = host_record.vps_ip or ""
        offline = OfflineHost(
            id=host_id,
            certified_host_data=host_record.certified_host_data,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data, vps_ip
            ),
        )
        self._evict_cached_host(host_id, replacement=offline)
        return offline

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData, vps_ip: str) -> None:
        """Callback when host data.json is updated -- sync to the unified host volume."""
        try:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                host_store = open_host_store(outer, host_volume_name_for(host_id))
                existing = host_store.read_host_record()
                if existing is not None:
                    updated = existing.model_copy(update={"certified_host_data": certified_data})
                    host_store.write_host_record(updated)
        except MngrError as e:
            logger.warning("Failed to sync certified data to VPS host volume: {}", e)

    # =========================================================================
    # VPS Provisioning
    # =========================================================================

    def _wait_for_cloud_init(self, outer: OuterHostInterface, timeout_seconds: float) -> None:
        """Wait for cloud-init to finish (Docker installed, marker file present)."""
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            if check_file_exists_on_outer(outer, Path("/var/run/mngr-ready")):
                elapsed = time.monotonic() - start
                if elapsed > 30.0:
                    logger.warning("Cloud-init took {:.1f}s (threshold: 30s)", elapsed)
                return
            time.sleep(5.0)
        raise MngrError(
            f"Cloud-init did not complete within {timeout_seconds}s. Docker may not be installed on the VPS."
        )

    def _wait_for_sshd_on_vps(self, vps_ip: str, timeout_seconds: float) -> None:
        """Wait for sshd on the VPS to be ready."""
        wait_for_sshd(hostname=vps_ip, port=22, timeout_seconds=timeout_seconds)

    # =========================================================================
    # Container Setup
    # =========================================================================

    def _setup_container_ssh(
        self,
        outer: OuterHostInterface,
        container_name: str,
        host_volume_mount_path: str | None,
        known_hosts_entries: tuple[str, ...],
        authorized_keys_entries: tuple[str, ...],
    ) -> None:
        """Set up SSH inside the container via docker exec.

        Delegates to the shared ``setup_container_ssh`` helper, supplying the
        container client/host keypairs this provider manages. The container is
        reached via <vps_ip>:<container_ssh_port> directly; the caller is
        responsible for adding the matching known_hosts entry once the VPS IP
        is known.
        """
        _container_key_path, container_public_key = self._get_container_ssh_keypair()
        container_host_key_path, container_host_public_key = self._get_container_host_keypair()
        setup_container_ssh(
            outer,
            container_name,
            mngr_host_dir=str(self.host_dir),
            host_volume_mount_path=host_volume_mount_path,
            container_public_key=container_public_key,
            container_host_private_key=container_host_key_path.read_text(),
            container_host_public_key=container_host_public_key,
            known_hosts_entries=known_hosts_entries,
            authorized_keys_entries=authorized_keys_entries,
        )

    def _prepare_btrfs_on_outer(self, outer: OuterHostInterface, host_id: HostId) -> Path:
        """Ensure btrfs loop FS + per-host subvolume exist on the outer; return the subvolume path.

        Thin wrapper around :func:`prepare_btrfs_on_outer` that pulls the
        loop-file path, mount path, and reserved-GB knob out of
        ``self.config``. See the free function for the full step-by-step.
        """
        return prepare_btrfs_on_outer(
            outer,
            host_id=host_id,
            btrfs_mount_path=self.config.btrfs_mount_path,
            loop_file_path=self.config.btrfs_loop_file_path,
            outer_disk_reserved_gb=self.config.outer_disk_reserved_gb,
        )

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
        logger.info("Creating VPS Docker host {} ({}) ...", name, host_id)

        parsed = self._parse_build_args(build_args)
        region, plan, os_id = parsed.region, parsed.plan, parsed.os_id

        _vps_key_path, vps_public_key = self._get_vps_ssh_keypair()
        vps_host_key_path, vps_host_public_key = self._get_vps_host_keypair()

        with log_span("Uploading SSH key to provider"):
            key_name = f"mngr-{self.name}-{host_id}"
            vps_ssh_key_id = self.vps_client.upload_ssh_key(key_name, vps_public_key)

        vps_instance_id: VpsInstanceId | None = None
        vps_ip: str | None = None
        try:
            vps_instance_id, vps_ip = self._provision_vps(
                host_id=host_id,
                name=name,
                region=region,
                plan=plan,
                os_id=os_id,
                vps_host_key_path=vps_host_key_path,
                vps_host_public_key=vps_host_public_key,
                vps_ssh_key_id=vps_ssh_key_id,
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
                    region=region,
                    plan=plan,
                    os_id=os_id,
                    image=image,
                    tags=tags,
                    build_args=build_args,
                    start_args=start_args,
                    lifecycle=lifecycle,
                    known_hosts=known_hosts,
                    authorized_keys=authorized_keys,
                )

            logger.info("VPS Docker host {} created successfully (VPS: {}, IP: {})", name, vps_instance_id, vps_ip)
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
        os_id: int | str,
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

        The unified host volume is provisioned at the top of
        ``_setup_container_on_vps`` -- that runs before the (potentially slow
        and failure-prone) image pull/build so the volume still exists by the
        time ``_finalize_host_creation`` writes ``host_state.json``.
        """
        base_image = str(image) if image else self.config.default_image
        # Prepend `--runtime <value>` (e.g. 'runsc' for gVisor) when configured; absent by default.
        runtime_args = ("--runtime", self.config.docker_runtime) if self.config.docker_runtime is not None else ()
        effective_start_args = runtime_args + tuple(self.config.default_start_args) + tuple(start_args or ())
        parsed = self._parse_build_args(build_args)

        container_name, container_id, volume_name = self._setup_container_on_vps(
            outer=outer,
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
        )

        return self._finalize_host_creation(
            host_id=host_id,
            name=name,
            vps_ip=vps_ip,
            outer=outer,
            container_name=container_name,
            container_id=container_id,
            volume_name=volume_name,
            base_image=base_image,
            effective_start_args=effective_start_args,
            tags=tags,
            lifecycle=lifecycle,
            region=region,
            plan=plan,
            os_id=os_id,
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

    def _provision_vps(
        self,
        host_id: HostId,
        name: HostName,
        region: str,
        plan: str,
        os_id: int | str,
        vps_host_key_path: Path,
        vps_host_public_key: str,
        vps_ssh_key_id: str,
    ) -> tuple[VpsInstanceId, str]:
        """Provision a VPS, wait for it to boot, and wait for Docker to install.

        Returns (vps_instance_id, vps_ip).
        """
        vps_host_private_key = vps_host_key_path.read_text()
        user_data = generate_cloud_init_user_data(
            host_private_key=vps_host_private_key,
            host_public_key=vps_host_public_key,
            install_gvisor_runtime=self.config.install_gvisor_runtime,
        )

        logger.log(LogLevel.BUILD.value, "Creating VPS instance (region: {}, plan: {})...", region, plan, source="vps")
        with log_span("Creating VPS instance"):
            vps_tags = build_vps_tags(host_id, self.name, os.environ.get("MNGR_VPS_EXTRA_TAGS", ""))
            vps_instance_id = self.vps_client.create_instance(
                label=f"mngr-{name}",
                region=region,
                plan=plan,
                os_id=os_id,
                user_data=user_data,
                ssh_key_ids=[vps_ssh_key_id],
                tags=vps_tags,
            )

        logger.log(LogLevel.BUILD.value, "Waiting for VPS to become active...", source="vps")
        with log_span("Waiting for VPS to become active"):
            vps_ip = self.vps_client.wait_for_instance_active(
                vps_instance_id,
                timeout_seconds=self.config.vps_boot_timeout,
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

        logger.log(LogLevel.BUILD.value, "Waiting for cloud-init to complete (Docker installation)...", source="vps")
        with log_span("Waiting for cloud-init (Docker install)"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                self._wait_for_cloud_init(outer, timeout_seconds=self.config.docker_install_timeout)
        logger.log(LogLevel.BUILD.value, "Cloud-init complete, Docker is ready", source="vps")

        return vps_instance_id, vps_ip

    def _setup_container_on_vps(
        self,
        outer: OuterHostInterface,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        base_image: str,
        effective_start_args: tuple[str, ...],
        docker_build_args: tuple[str, ...],
        git_depth: int | None,
        tags: Mapping[str, str] | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
    ) -> tuple[str, str, str]:
        """Create the Docker container and configure SSH inside it.

        If docker_build_args are provided, uploads the build context to the VPS
        and runs docker build there. Otherwise pulls the base image directly.

        Returns (container_name, container_id, volume_name).

        The btrfs loop FS and the per-host unified docker named volume are
        provisioned at the top of this method, before the (slow, failure-prone)
        image pull/build, so the bind-source path always exists by the time
        ``_finalize_host_creation`` writes ``host_state.json`` -- even if a
        later step in this method fails.
        """
        volume_name = host_volume_name_for(host_id)
        snapshot_trigger_volume_name = snapshot_trigger_volume_name_for(host_id)

        with log_span("Provisioning unified host volume on btrfs subvolume"):
            subvolume_path = self._prepare_btrfs_on_outer(outer, host_id)
            seed_host_volume_layout_on_outer(outer, subvolume_path)
            create_bind_volume_on_outer(outer, volume_name=volume_name, device_path=subvolume_path)

        # Snapshot helper: lets the in-container host_backup service request
        # `btrfs subvolume snapshot` against the per-host subvolume via a
        # request.json / result.json file protocol in a dedicated docker
        # volume. Both the systemd unit on the outer and the docker volume
        # mounted at /mngr-snapshot/ in the container need to exist before
        # the agent boots. The helper provisioning is internally a 2-phase
        # parallel pipeline (see provision_snapshot_helper_on_outer) so
        # the ~7 SSH round-trips it would otherwise serialize collapse to
        # the latency of 2.
        provision_snapshot_helper_on_outer(
            outer,
            self.mngr_ctx.concurrency_group,
            host_id=host_id,
            btrfs_mount_path=self.config.btrfs_mount_path,
            subvolume_path=subvolume_path,
            trigger_volume_name=snapshot_trigger_volume_name,
        )

        if docker_build_args:
            base_image = self._build_image_on_vps(outer, host_id, base_image, docker_build_args, git_depth)
        else:
            logger.log(LogLevel.BUILD.value, "Pulling Docker image {} on VPS...", base_image, source="vps")
            with log_span("Pulling Docker image on VPS"):
                pull_image(outer, base_image, timeout_seconds=300.0)

        container_name = f"{self.mngr_ctx.config.prefix}{name}"
        labels = {
            LABEL_HOST_ID: str(host_id),
            LABEL_HOST_NAME: str(name),
            LABEL_PROVIDER: str(self.name),
            LABEL_TAGS: json.dumps(dict(tags) if tags else {}),
        }
        logger.log(LogLevel.BUILD.value, "Starting Docker container on VPS...", source="vps")
        snapshots_dir_on_outer = self.config.btrfs_mount_path / "snapshots"
        with log_span("Starting Docker container"):
            container_id = run_container(
                outer,
                image=base_image,
                name=container_name,
                port_mappings={f"0.0.0.0:{self.config.container_ssh_port}": "22"},
                volumes=[
                    f"{volume_name}:{HOST_VOLUME_MOUNT_PATH}:rw",
                    # Snapshot helper IPC volume (bind-shared with the outer
                    # at OUTER_SNAPSHOT_TRIGGER_DIR via the named volume created
                    # above; host_backup writes request.json / reads result.json).
                    f"{snapshot_trigger_volume_name}:{SNAPSHOT_TRIGGER_MOUNT_PATH}:rw",
                    # Read-only view of the outer's <btrfs-mount>/snapshots/
                    # directory so restic-in-container can read the snapshot
                    # the outer helper produced at <btrfs-mount>/snapshots/current.
                    f"{snapshots_dir_on_outer}:{SNAPSHOT_READ_MOUNT_PATH}:ro",
                ],
                labels=labels,
                extra_args=list(effective_start_args),
                entrypoint_cmd=CONTAINER_ENTRYPOINT_CMD,
            )

        logger.log(LogLevel.BUILD.value, "Setting up SSH in container...", source="vps")
        with log_span("Setting up SSH in container"):
            self._setup_container_ssh(
                outer=outer,
                container_name=container_name,
                host_volume_mount_path=f"{HOST_VOLUME_MOUNT_PATH}/{HOST_DIR_SUBPATH}",
                known_hosts_entries=tuple(known_hosts or ()),
                authorized_keys_entries=tuple(authorized_keys or ()),
            )

        _container_host_key_path, container_host_public_key = self._get_container_host_keypair()
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            public_key=container_host_public_key,
        )
        logger.log(LogLevel.BUILD.value, "Waiting for container SSH to be ready...", source="vps")
        with log_span("Waiting for container SSH"):
            self._wait_for_container_sshd(vps_ip)
        logger.log(LogLevel.BUILD.value, "Container SSH ready", source="vps")

        return container_name, container_id, volume_name

    def _finalize_host_creation(
        self,
        host_id: HostId,
        name: HostName,
        vps_ip: str,
        outer: OuterHostInterface,
        container_name: str,
        container_id: str,
        volume_name: str,
        base_image: str,
        effective_start_args: tuple[str, ...],
        tags: Mapping[str, str] | None,
        lifecycle: HostLifecycleOptions | None,
        region: str,
        plan: str,
        os_id: int | str,
        vps_instance_id: VpsInstanceId,
        vps_ssh_key_id: str,
        vps_host_public_key: str,
    ) -> Host:
        """Create the Host object, configure activity watching, and persist state."""
        host = self._create_host_object(host_id, name, vps_ip)

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
            start_watcher_cmd = build_start_activity_watcher_command(str(self.host_dir))
            exec_in_container(outer, container_name, start_watcher_cmd)

        host_record = VpsDockerHostRecord(
            certified_host_data=host_data,
            vps_ip=vps_ip,
            ssh_host_public_key=vps_host_public_key,
            container_ssh_host_public_key=self._get_container_host_keypair()[1],
            config=VpsHostConfig(
                vps_instance_id=vps_instance_id,
                region=region,
                plan=plan,
                os_id=os_id,
                start_args=effective_start_args,
                image=base_image,
                container_name=container_name,
                volume_name=volume_name,
                vps_ssh_key_id=vps_ssh_key_id,
            ),
            container_id=container_id,
        )
        host_store = open_host_store(outer, volume_name)
        host_store.write_host_record(host_record)

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

        Default no-op. Must not raise; any errors should be caught and
        logged by the override.
        """

    def _wait_for_container_sshd(self, vps_ip: str) -> None:
        """Wait for sshd in the container to be reachable via the VPS's exposed port."""
        wait_for_sshd(
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            timeout_seconds=self.config.ssh_connect_timeout,
        )

    def _build_image_on_vps(
        self,
        outer: OuterHostInterface,
        host_id: HostId,
        base_image: str,
        docker_build_args: tuple[str, ...],
        git_depth: int | None,
    ) -> str:
        """Build a Docker image on the VPS from the provided build args.

        Thin wrapper around the shared ``build_image_on_outer_from_build_args``
        helper, supplying this provider's configured docker builder. Returns the
        image tag to use.
        """
        return build_image_on_outer_from_build_args(
            outer,
            self.mngr_ctx.concurrency_group,
            host_id=host_id,
            docker_build_args=docker_build_args,
            git_depth=git_depth,
            builder=self.config.builder,
        )

    def _create_shutdown_script(self, host: Host) -> None:
        """Create the shutdown script that stops the container on idle."""
        shutdown_script = "#!/bin/bash\nkill -TERM 1\n"
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", shutdown_script.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        """Parse build args, separating VPS provisioning args from Docker build args."""
        return _parse_build_args(
            build_args,
            default_region=self.config.default_region,
            default_plan=self.config.default_plan,
            default_os_id=self.config.default_os_id,
        )

    # =========================================================================
    # Core Lifecycle: stop_host
    # =========================================================================

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)

        if create_snapshot:
            try:
                self.create_snapshot(host_id)
            except MngrError as e:
                logger.warning("Failed to create snapshot before stop: {}", e)

        # Disconnect SSH before stopping (also disconnect the passed-in host
        # in case it is a different instance than the cached one).
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            with log_span("Stopping container on VPS"):
                stop_container(outer, host_record.config.container_name, timeout_seconds=int(timeout_seconds))

            # Update host record
            host_store = open_host_store(outer, host_record.config.volume_name)
            now = datetime.now(timezone.utc)
            updated_data = host_record.certified_host_data.model_copy(update={"updated_at": now})
            updated_record = host_record.model_copy(update={"certified_host_data": updated_data})
            host_store.write_host_record(updated_record)

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

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            with log_span("Starting container on VPS"):
                start_container(outer, host_record.config.container_name)

        # Wait for sshd in container
        with log_span("Waiting for container SSH"):
            self._wait_for_container_sshd(host_record.vps_ip)

        host_obj = self._create_host_object(
            host_id, HostName(host_record.certified_host_data.host_name), host_record.vps_ip
        )
        logger.info("Host {} started", host_id)
        return host_obj

    # =========================================================================
    # Core Lifecycle: destroy_host
    # =========================================================================

    def destroy_host(self, host: HostInterface | HostId) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host

        # Disconnect SSH before destroying (also disconnect the passed-in host
        # in case it is a different instance than the cached one).
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        vps_config = host_record.config
        vps_ip = host_record.vps_ip

        if vps_ip is not None:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                # Stop and remove the agent container; removing the volume below
                # will fail otherwise because the container still holds it open.
                try:
                    remove_container(outer, vps_config.container_name, force=True)
                except MngrError as e:
                    logger.warning("Failed to remove container: {}", e)

                # Delete the per-host btrfs subvolume before the named volume.
                # The VPS-destroy that follows takes the whole loop file with it,
                # so this is primarily belt-and-suspenders for the rare case of
                # a destroy retried on a still-existing VPS (e.g. the operator
                # re-runs `mngr destroy` after VPS termination has failed).
                subvolume_path = self.config.btrfs_mount_path / host_id.get_uuid().hex
                try:
                    delete_btrfs_subvolume_on_outer(outer, subvolume_path)
                except MngrError as e:
                    logger.warning("Failed to delete btrfs subvolume {}: {}", subvolume_path, e)

                # Remove the unified host volume. With bind options the volume
                # itself holds no data (the subvolume above did), but the named
                # entry still needs cleanup so a later create with the same
                # volume name doesn't collide.
                try:
                    remove_volume(outer, vps_config.volume_name)
                except MngrError as e:
                    logger.warning("Failed to remove host volume: {}", e)

                # Remove the per-host snapshot-trigger volume (the named entry;
                # the bind source at OUTER_SNAPSHOT_TRIGGER_DIR is shared across
                # all containers on this outer and is left alone).
                try:
                    remove_volume(outer, snapshot_trigger_volume_name_for(host_id))
                except MngrError as e:
                    logger.warning("Failed to remove snapshot trigger volume: {}", e)

        # Destroy the VPS instance
        with log_span("Destroying VPS instance"):
            try:
                self.vps_client.destroy_instance(vps_config.vps_instance_id)
            except Exception as e:
                logger.warning("Failed to destroy VPS: {}", e)

        # Clean up SSH key from provider
        if vps_config.vps_ssh_key_id is not None:
            try:
                self.vps_client.delete_ssh_key(vps_config.vps_ssh_key_id)
            except Exception as e:
                logger.warning("Failed to delete SSH key from provider: {}", e)

        # Clean up local known_hosts
        if vps_ip is not None:
            try:
                remove_host_from_known_hosts(self._vps_known_hosts_path(), vps_ip, 22)
            except Exception as e:
                logger.trace("Failed to clean up VPS known_hosts: {}", e)
            try:
                remove_host_from_known_hosts(
                    self._container_known_hosts_path(), vps_ip, self.config.container_ssh_port
                )
            except Exception as e:
                logger.trace("Failed to clean up container known_hosts: {}", e)

        logger.info("Host {} destroyed (VPS {})", host_id, vps_config.vps_instance_id)

    def delete_host(self, host: HostInterface) -> None:
        """Delete all local records for a destroyed host (does not destroy VPS)."""
        self._evict_cached_host(host.id)

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

        if vps_ip is not None and host_record.config is not None:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                # Check if container is running
                if docker_inspect_running(outer, host_record.config.container_name):
                    return self._create_host_object(
                        host_id, HostName(host_record.certified_host_data.host_name), vps_ip
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
                with self._make_outer_for_vps_ip(record.vps_ip) as outer:
                    if docker_inspect_running(outer, record.config.container_name):
                        self._create_host_object(host_id, host_name, record.vps_ip)
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
        """Load hosts and agent references from each VPS's unified host volume.

        For each VPS, reads the host record and any persisted agent data
        directly from the docker volume's bind-source path (the per-host
        btrfs subvolume the volume's ``Options.device`` points at), then
        determines container running status. Avoids the default
        implementation's per-host SSH calls into containers for agent
        discovery.
        """
        with log_span("VPS Docker discover_hosts_and_agents for provider={}", self.name):
            all_records, agent_data_by_host_id = self._discover_host_records_with_agents()

        result: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
        for record in all_records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)

            # Cache the host record for later use by get_host_and_agent_details
            self._host_record_cache[host_id] = record

            # Determine host state from container running status. If the VPS
            # is unreachable, we trust the offline-state derivation rather
            # than crashing the listing -- one bad VPS must not drop the
            # other VPSes' hosts.
            is_running = False
            if record.vps_ip is not None and record.config is not None:
                container_name = record.config.container_name
                if container_name not in self._container_running_cache:
                    try:
                        with self._make_outer_for_vps_ip(record.vps_ip) as outer:
                            self._container_running_cache[container_name] = docker_inspect_running(
                                outer, container_name
                            )
                    except MngrError as exc:
                        logger.warning(
                            "Failed to inspect container status for host {} on VPS {}: {}",
                            host_id,
                            record.vps_ip,
                            exc,
                        )
                        self._container_running_cache[container_name] = False
                is_running = self._container_running_cache[container_name]

            has_snapshots = len(record.certified_host_data.snapshots) > 0
            is_failed = record.certified_host_data.failure_reason is not None

            if not is_running and not is_failed and not has_snapshots and not include_destroyed:
                continue

            if is_running and record.vps_ip is not None:
                host_state = HostState.RUNNING
                self._create_host_object(host_id, host_name, record.vps_ip)
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

            # Build agent refs from persisted agent data
            agent_refs: list[DiscoveredAgent] = []
            for agent_data in agent_data_by_host_id.get(host_id, []):
                ref = validate_and_create_discovered_agent(agent_data, host_id, self.name)
                if ref is not None:
                    agent_refs.append(ref)

            result[host_ref] = agent_refs

        return result

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return SSH-reachable hostnames for VPSes owned by this provider instance.

        Each entry is whatever the provider hands back as the SSH target
        for one of its VPSes -- a public IPv4 (Vultr) or a provider DNS
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

    def _read_records_from_vps(
        self,
        vps_ip: str,
    ) -> tuple[list[VpsDockerHostRecord], dict[HostId, list[dict[str, Any]]]]:
        """Read the (single) host record + agent data from one VPS.

        Each VPS hosts exactly one mngr container (1:1 invariant). We find
        that container by its host-id label, derive its unified volume name,
        and read host_state.json + agents/*.json from the volume's
        bind-source path (the per-host btrfs subvolume the volume's
        ``Options.device`` points at, resolved via
        ``docker volume inspect --format '{{.Options.device}}'``; the
        docker-managed ``Mountpoint`` placeholder is never consulted). If
        the container does not exist yet (e.g., the VPS is still being set
        up by a concurrent ``mngr create``), returns empty results. If
        outer SSH to the VPS fails, fall back to any in-process cached
        records for that VPS so the hosts still appear in the listing
        (with an offline state) instead of disappearing entirely; one bad
        VPS must not silently drop its hosts.
        """
        try:
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                host_id = _read_host_id_label_from_vps(outer)
                if host_id is None:
                    logger.debug("No mngr container on VPS {} yet, skipping", vps_ip)
                    return [], {}
                host_store = open_host_store(outer, host_volume_name_for(host_id))
                record = host_store.read_host_record()
                if record is None:
                    logger.debug("No host record on VPS {} volume yet, skipping", vps_ip)
                    return [], {}
                agent_data = host_store.list_persisted_agent_data()
                return [record], {host_id: agent_data}
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
            return cached_records, {}

    def _discover_host_records_with_agents(
        self,
    ) -> tuple[list[VpsDockerHostRecord], dict[HostId, list[dict[str, Any]]]]:
        """Discover host records and agent data from all VPSes for this provider.

        Calls ``_list_provider_vps_hostnames`` to enumerate VPSes
        (provider-specific), then SSHes to each in parallel. Within each
        VPS, ``_read_records_from_vps`` issues a small sequence of SSH
        commands (find the mngr container by host-id label, resolve the
        unified volume's bind-source path via ``docker volume inspect
        --format '{{.Options.device}}'``, read ``host_state.json``, list
        ``agents/*.json``); the parallel fan-out across VPSes keeps wall
        time bounded by the slowest VPS rather than the sum.
        """
        vps_ips = self._list_provider_vps_hostnames()
        if not vps_ips:
            return [], {}

        all_records: list[VpsDockerHostRecord] = []
        all_agent_data: dict[HostId, list[dict[str, Any]]] = {}

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
                    records, agent_data = future.result()
                    all_records.extend(records)
                    for host_id, agents in agent_data.items():
                        all_agent_data.setdefault(host_id, []).extend(agents)

        return all_records, all_agent_data

    def _discover_host_records(self) -> list[VpsDockerHostRecord]:
        """Discover host records by enumerating this provider's VPSes."""
        records, _agent_data = self._discover_host_records_with_agents()
        return records

    def _find_host_record(self, host: HostId | HostName) -> VpsDockerHostRecord | None:
        """Find a host record by ID or name, using cache first."""
        if isinstance(host, HostId) and host in self._host_record_cache:
            return self._host_record_cache[host]
        if isinstance(host, HostName):
            for cached_record in self._host_record_cache.values():
                if cached_record.certified_host_data.host_name == str(host):
                    return cached_record

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
                    raw_output = exec_in_container(
                        outer,
                        host_record.config.container_name,
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
        host_record: VpsDockerHostRecord,
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
                command=build_user_ssh_command(user, hostname, port, key_path),
            )

        boot_time = timestamp_to_datetime(raw.get("btime"))
        uptime_seconds = raw.get("uptime_seconds")
        resource = self.get_host_resources(host)

        lock_mtime = raw.get("lock_mtime")
        is_locked = lock_mtime is not None
        locked_time = datetime.fromtimestamp(lock_mtime, tz=timezone.utc) if lock_mtime is not None else None

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
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)

        snapshot_name = name or SnapshotName(f"mngr-snapshot-{host_id}-{int(time.time())}")
        image_tag = f"mngr-snapshot-{host_id.get_uuid().hex}-{int(time.time())}"

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            with log_span("Creating Docker snapshot"):
                image_id = commit_container(outer, host_record.config.container_name, image_tag)

            # Store snapshot record in host data
            snapshot_record = SnapshotRecord(
                id=image_id,
                name=str(snapshot_name),
                created_at=datetime.now(timezone.utc).isoformat(),
            )

            # Update certified data with new snapshot
            existing_snapshots = host_record.certified_host_data.snapshots
            updated_snapshots = list(existing_snapshots) + [snapshot_record]
            updated_data = host_record.certified_host_data.model_copy(
                update={"snapshots": updated_snapshots, "updated_at": datetime.now(timezone.utc)}
            )
            updated_record = host_record.model_copy(update={"certified_host_data": updated_data})

            # ``host_record.config`` is guaranteed non-None by the guard at the top of this method.
            host_store = open_host_store(outer, host_record.config.volume_name)
            host_store.write_host_record(updated_record)

        logger.info("Created snapshot {} for host {}", snapshot_name, host_id)
        return SnapshotId(image_id)

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
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            try:
                run_docker(outer, ["rmi", str(snapshot_id)])
            except MngrError as e:
                logger.warning("Failed to delete snapshot image: {}", e)

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
        raise MngrError("VPS Docker provider does not support mutable tags")

    def add_tags_to_host(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("VPS Docker provider does not support mutable tags")

    def remove_tags_from_host(self, host: HostInterface | HostId, keys: Sequence[str]) -> None:
        raise MngrError("VPS Docker provider does not support mutable tags")

    def rename_host(self, host: HostInterface | HostId, name: HostName) -> HostInterface:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)

        updated_data = host_record.certified_host_data.model_copy(
            update={"host_name": str(name), "updated_at": datetime.now(timezone.utc)}
        )
        updated_record = host_record.model_copy(update={"certified_host_data": updated_data})

        if host_record.vps_ip is not None:
            if host_record.config is None:
                raise MngrError(
                    f"Host record for {host_id} on VPS {host_record.vps_ip} is missing config -- "
                    "cannot determine unified volume name to rename"
                )
            with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
                host_store = open_host_store(outer, host_record.config.volume_name)
                host_store.write_host_record(updated_record)

        return self.get_host(host_id)

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
            host_store = open_host_store(outer, host_record.config.volume_name)
            return host_store.list_persisted_agent_data()

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = open_host_store(outer, host_record.config.volume_name)
            host_store.persist_agent_data(agent_data)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.vps_ip is None or host_record.config is None:
            raise HostNotFoundError(self.name, host_id)

        with self._make_outer_for_vps_ip(host_record.vps_ip) as outer:
            host_store = open_host_store(outer, host_record.config.volume_name)
            host_store.remove_persisted_agent_data(agent_id)
