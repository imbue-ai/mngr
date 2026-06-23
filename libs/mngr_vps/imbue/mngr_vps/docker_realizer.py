import json
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.cleanup_failures import collecting_cleanup_failures
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.listing_utils import build_outer_listing_collection_script
from imbue.mngr.providers.listing_utils import extract_agent_data_from_parsed_listing
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr_vps.container_setup import CONTAINER_ENTRYPOINT_CMD
from imbue.mngr_vps.container_setup import HOST_DIR_SUBPATH
from imbue.mngr_vps.container_setup import HOST_VOLUME_MOUNT_PATH
from imbue.mngr_vps.container_setup import LABEL_HOST_ID
from imbue.mngr_vps.container_setup import LABEL_HOST_NAME
from imbue.mngr_vps.container_setup import LABEL_PROVIDER
from imbue.mngr_vps.container_setup import LABEL_TAGS
from imbue.mngr_vps.container_setup import SNAPSHOT_READ_MOUNT_PATH
from imbue.mngr_vps.container_setup import SNAPSHOT_TRIGGER_MOUNT_PATH
from imbue.mngr_vps.container_setup import build_image_on_outer_from_build_args
from imbue.mngr_vps.container_setup import commit_container
from imbue.mngr_vps.container_setup import create_bind_volume_on_outer
from imbue.mngr_vps.container_setup import delete_btrfs_subvolume_on_outer
from imbue.mngr_vps.container_setup import docker_inspect_running
from imbue.mngr_vps.container_setup import exec_in_container
from imbue.mngr_vps.container_setup import host_volume_name_for
from imbue.mngr_vps.container_setup import is_running_container_state
from imbue.mngr_vps.container_setup import prepare_btrfs_on_outer
from imbue.mngr_vps.container_setup import provision_snapshot_helper_on_outer
from imbue.mngr_vps.container_setup import pull_image
from imbue.mngr_vps.container_setup import remove_container
from imbue.mngr_vps.container_setup import remove_volume
from imbue.mngr_vps.container_setup import run_container
from imbue.mngr_vps.container_setup import run_docker
from imbue.mngr_vps.container_setup import seed_host_volume_layout_on_outer
from imbue.mngr_vps.container_setup import setup_container_ssh
from imbue.mngr_vps.container_setup import snapshot_trigger_volume_name_for
from imbue.mngr_vps.container_setup import start_container
from imbue.mngr_vps.container_setup import start_container_sshd
from imbue.mngr_vps.container_setup import stop_container
from imbue.mngr_vps.data_types import AgentEndpoint
from imbue.mngr_vps.data_types import PlacementHandle
from imbue.mngr_vps.data_types import RealizePlacementContext
from imbue.mngr_vps.data_types import RealizedPlacement
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.host_store import VpsHostStore
from imbue.mngr_vps.host_store import open_host_store
from imbue.mngr_vps.interfaces import SnapshotCapableRealizer
from imbue.mngr_vps.primitives import load_or_create_per_host_host_keypair

# Key-file names under ``key_dir`` for the container's client/host keys and its
# known_hosts. The provider exposes thin accessors with the same names so the
# imbue_cloud slice provider's ``_create_host_object`` override keeps working;
# these constants are the single source of truth for the file names.
CONTAINER_SSH_KEY_NAME: Final[str] = "container_ssh_key"
CONTAINER_HOST_KEY_NAME: Final[str] = "container_host_key"
CONTAINER_KNOWN_HOSTS_NAME: Final[str] = "container_known_hosts"


def _read_host_id_label_from_vps(outer: OuterHostInterface) -> HostId | None:
    """Return the host_id label of the (single) mngr container on this VPS, if any.

    Each VPS hosts at most one mngr container (1:1 invariant), so the value of
    the ``com.imbue.mngr.host-id`` label on any container with that label set
    uniquely identifies the VPS's host. Returns ``None`` when no such container
    exists yet. Includes stopped containers so a paused host is still discoverable.
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
            # A corrupted/manually-edited label must not crash discovery for the
            # whole VPS; surface as MngrError so the provider's fallback path logs
            # and continues.
            raise MngrError(f"Container on VPS has malformed {LABEL_HOST_ID} label {value!r}: {e}") from e
    return None


def _read_live_listing_from_vps(
    outer: OuterHostInterface, host_id: HostId, host_dir: str, prefix: str, window_name: str
) -> dict[str, Any]:
    """Run the outer listing script on the VPS and return the parsed live listing.

    Reads agent state directly from the running container's live ``host_dir`` (or,
    for a stopped container, from a ``docker cp``-extracted copy), so agents
    created *inside* the container are discovered.
    """
    script = build_outer_listing_collection_script(
        str(host_id), host_dir, prefix, host_id_label=LABEL_HOST_ID, window_name=window_name
    )
    result = outer.execute_idempotent_command(script, timeout_seconds=60.0)
    if not result.success:
        raise MngrError(
            f"Outer listing script failed on VPS for host {host_id}: "
            f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
    return parse_listing_collection_output(result.stdout)


def _record_cleanup_attempt(
    failures: list[CleanupFailure],
    host_id: HostId,
    op: Callable[[], None],
    *,
    action: str,
    resource: str,
) -> None:
    """Run one best-effort teardown step; record a HOST_RESOURCE_REMAINS failure if it raises.

    Each ``op`` already no-ops on an already-absent resource, so a raised
    ``MngrError`` means the resource exists but could not be removed. The failure is
    logged and appended to ``failures`` (which ``collecting_cleanup_failures`` raises
    as a group at the end) rather than aborting the remaining teardown steps.
    """
    try:
        op()
    except MngrError as e:
        logger.warning("Failed to {} {}: {}", action, resource, e)
        failures.append(
            CleanupFailure(
                category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                message=f"failed to {action} {resource} for host {host_id}: {e}",
                host_id=host_id,
            )
        )


class DockerRealizer(SnapshotCapableRealizer):
    """Places the agent inside a Docker container on the VPS.

    This is the original ``VpsProvider`` behavior, moved behind the
    ``HostRealizer`` seam: build/pull the image, run the container with the
    unified btrfs-backed volume and snapshot helper, configure the container's
    sshd, and manage container stop/start/teardown plus ``docker commit``
    snapshots. The agent is reached at ``vps_ip:container_ssh_port``.
    """

    @property
    def idle_shutdown_command(self) -> str:
        # Signal the container's PID 1 to stop the container. On a self-stopping
        # cloud substrate (aws/gcp/azure) the provider overrides the shutdown
        # handling with a sentinel + host-side watcher, since a container can't
        # power off its host from the inside.
        return "kill -TERM 1"

    @property
    def idle_shutdown_stops_host(self) -> bool:
        return False

    def host_dir_path_on_outer(self, host_id: HostId) -> Path:
        # The per-host host_dir lives on the btrfs subvolume the unified volume binds to.
        return self.config.btrfs_mount_path / host_id.get_uuid().hex / HOST_DIR_SUBPATH

    # --- container identity (keys + known_hosts) ---------------------------

    def _container_ssh_keypair(self) -> tuple[Path, str]:
        return load_or_create_ssh_keypair(self.key_dir, CONTAINER_SSH_KEY_NAME)

    def _container_host_keypair(self, host_id: HostId) -> tuple[Path, str]:
        # Per host: each container gets its own sshd host key so one host's key can
        # never be reused to impersonate another. Matches the provider's
        # ``_get_container_host_keypair(host_id)`` path (same key_dir), so the key
        # this realizer injects is the one the provider later reads + pins.
        return load_or_create_per_host_host_keypair(self.key_dir, host_id, CONTAINER_HOST_KEY_NAME)

    def _container_known_hosts_path(self) -> Path:
        return self.key_dir / CONTAINER_KNOWN_HOSTS_NAME

    def agent_endpoint(self, vps_ip: str) -> AgentEndpoint:
        container_key_path, _container_pub = self._container_ssh_keypair()
        return AgentEndpoint(
            hostname=vps_ip,
            port=self.config.container_ssh_port,
            private_key_path=container_key_path,
            known_hosts_path=self._container_known_hosts_path(),
        )

    def open_host_store(self, outer: OuterHostInterface, host_id: HostId) -> VpsHostStore:
        return open_host_store(outer, host_volume_name_for(host_id))

    # --- discovery / listing ----------------------------------------------

    def find_host_record(self, outer: OuterHostInterface) -> tuple[HostId, VpsHostRecord] | None:
        host_id = _read_host_id_label_from_vps(outer)
        if host_id is None:
            return None
        record = self.open_host_store(outer, host_id).read_host_record()
        if record is None:
            return None
        return host_id, record

    def read_live_listing(
        self, outer: OuterHostInterface, host_id: HostId, host_dir: str, prefix: str, window_name: str
    ) -> tuple[list[dict[str, Any]], bool]:
        parsed = _read_live_listing_from_vps(outer, host_id, host_dir, prefix, window_name)
        return extract_agent_data_from_parsed_listing(parsed), is_running_container_state(
            parsed.get("container_state")
        )

    @staticmethod
    def _require_container_name(handle: PlacementHandle) -> str:
        """The container name the docker realizer needs to act on its placement."""
        assert handle.container_name is not None, "DockerRealizer placement handle has no container name"
        return handle.container_name

    def is_placement_running(self, outer: OuterHostInterface, handle: PlacementHandle) -> bool:
        return docker_inspect_running(outer, self._require_container_name(handle))

    def collect_listing_output(
        self, outer: OuterHostInterface, handle: PlacementHandle, script: str, timeout_seconds: float = 30.0
    ) -> str:
        return exec_in_container(outer, self._require_container_name(handle), script, timeout_seconds=timeout_seconds)

    # --- placement creation ------------------------------------------------

    def _setup_container_ssh(
        self,
        outer: OuterHostInterface,
        host_id: HostId,
        container_name: str,
        host_volume_mount_path: str | None,
        known_hosts_entries: tuple[str, ...],
        authorized_keys_entries: tuple[str, ...],
    ) -> None:
        """Set up SSH inside the container via docker exec."""
        _container_key_path, container_public_key = self._container_ssh_keypair()
        container_host_key_path, container_host_public_key = self._container_host_keypair(host_id)
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
        """Ensure btrfs loop FS + per-host subvolume exist on the outer; return the subvolume path."""
        return prepare_btrfs_on_outer(
            outer,
            host_id=host_id,
            btrfs_mount_path=self.config.btrfs_mount_path,
            loop_file_path=self.config.btrfs_loop_file_path,
            outer_disk_reserved_gb=self.config.outer_disk_reserved_gb,
        )

    def _build_image_on_vps(
        self,
        outer: OuterHostInterface,
        host_id: HostId,
        base_image: str,
        docker_build_args: tuple[str, ...],
        git_depth: int | None,
    ) -> str:
        """Build a Docker image on the VPS from the provided build args."""
        return build_image_on_outer_from_build_args(
            outer,
            self.mngr_ctx.concurrency_group,
            host_id=host_id,
            docker_build_args=docker_build_args,
            git_depth=git_depth,
            builder=self.config.builder,
        )

    def realize_placement(self, outer: OuterHostInterface, ctx: RealizePlacementContext) -> RealizedPlacement:
        host_id = ctx.host_id
        volume_name = host_volume_name_for(host_id)
        snapshot_trigger_volume_name = snapshot_trigger_volume_name_for(host_id)

        with log_span("Provisioning unified host volume on btrfs subvolume"):
            subvolume_path = self._prepare_btrfs_on_outer(outer, host_id)
            seed_host_volume_layout_on_outer(outer, subvolume_path)
            create_bind_volume_on_outer(outer, volume_name=volume_name, device_path=subvolume_path)

        # Snapshot helper: lets the in-container host_backup service request
        # `btrfs subvolume snapshot` against the per-host subvolume via a
        # request.json / result.json file protocol in a dedicated docker volume.
        provision_snapshot_helper_on_outer(
            outer,
            self.mngr_ctx.concurrency_group,
            host_id=host_id,
            btrfs_mount_path=self.config.btrfs_mount_path,
            subvolume_path=subvolume_path,
            trigger_volume_name=snapshot_trigger_volume_name,
        )

        image = ctx.base_image
        if ctx.docker_build_args:
            image = self._build_image_on_vps(outer, host_id, image, ctx.docker_build_args, ctx.git_depth)
        else:
            logger.log(LogLevel.BUILD.value, "Pulling Docker image {} on VPS...", image, source="vps")
            with log_span("Pulling Docker image on VPS"):
                pull_image(outer, image, timeout_seconds=300.0)

        container_name = f"{self.mngr_ctx.config.prefix}{ctx.name}"
        labels = {
            LABEL_HOST_ID: str(host_id),
            LABEL_HOST_NAME: str(ctx.name),
            LABEL_PROVIDER: str(self.provider_name),
            LABEL_TAGS: json.dumps(dict(ctx.tags) if ctx.tags else {}),
        }
        logger.log(LogLevel.BUILD.value, "Starting Docker container on VPS...", source="vps")
        snapshots_dir_on_outer = self.config.btrfs_mount_path / "snapshots"
        with log_span("Starting Docker container"):
            container_id = run_container(
                outer,
                image=image,
                name=container_name,
                port_mappings={f"0.0.0.0:{self.config.container_ssh_port}": "22"},
                volumes=[
                    f"{volume_name}:{HOST_VOLUME_MOUNT_PATH}:rw",
                    # Snapshot helper IPC volume (host_backup writes request.json / reads result.json).
                    f"{snapshot_trigger_volume_name}:{SNAPSHOT_TRIGGER_MOUNT_PATH}:rw",
                    # Read-only view of the outer's <btrfs-mount>/snapshots/ directory.
                    f"{snapshots_dir_on_outer}:{SNAPSHOT_READ_MOUNT_PATH}:ro",
                ],
                labels=labels,
                extra_args=list(ctx.effective_start_args),
                entrypoint_cmd=CONTAINER_ENTRYPOINT_CMD,
            )

        logger.log(LogLevel.BUILD.value, "Setting up SSH in container...", source="vps")
        with log_span("Setting up SSH in container"):
            self._setup_container_ssh(
                outer=outer,
                host_id=ctx.host_id,
                container_name=container_name,
                host_volume_mount_path=f"{HOST_VOLUME_MOUNT_PATH}/{HOST_DIR_SUBPATH}",
                known_hosts_entries=tuple(ctx.known_hosts or ()),
                authorized_keys_entries=tuple(ctx.authorized_keys or ()),
            )

        _container_host_key_path, container_host_public_key = self._container_host_keypair(ctx.host_id)
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname=ctx.vps_ip,
            port=self.config.container_ssh_port,
            public_key=container_host_public_key,
        )
        return RealizedPlacement(
            handle=PlacementHandle(
                container_name=container_name,
                container_id=container_id,
                volume_name=volume_name,
            ),
            container_ssh_host_public_key=container_host_public_key,
        )

    # --- placement lifecycle ----------------------------------------------

    def start_activity_watcher(self, outer: OuterHostInterface, handle: PlacementHandle) -> None:
        container_name = self._require_container_name(handle)
        exec_in_container(outer, container_name, build_start_activity_watcher_command(str(self.host_dir)))

    def stop_placement(self, outer: OuterHostInterface, handle: PlacementHandle, timeout_seconds: float) -> None:
        with log_span("Stopping container on VPS"):
            stop_container(outer, self._require_container_name(handle), timeout_seconds=int(timeout_seconds))

    def start_placement(self, outer: OuterHostInterface, handle: PlacementHandle) -> None:
        container_name = self._require_container_name(handle)
        with log_span("Starting container on VPS"):
            start_container(outer, container_name)
        # sshd is launched via `docker exec`, not the container's entrypoint, so a
        # `docker start` brings the container back WITHOUT sshd. Re-exec it before
        # the provider waits for sshd. `docker start` is a no-op on an already-running
        # container, so this also repairs the container-up-but-sshd-down state.
        with log_span("Restarting sshd in container"):
            start_container_sshd(outer, container_name)

    def teardown_placement(self, outer: OuterHostInterface, host_id: HostId, handle: PlacementHandle) -> None:
        with collecting_cleanup_failures() as failures:
            # Stop and remove the agent container; removing the volume below will
            # fail otherwise because the container still holds it open.
            # ``tolerate_missing`` makes an already-gone container a no-op, so any
            # error raised here means a container that exists but could not be removed.
            container_name = handle.container_name
            if container_name is not None:
                _record_cleanup_attempt(
                    failures,
                    host_id,
                    lambda: remove_container(outer, container_name, force=True, tolerate_missing=True),
                    action="remove container",
                    resource=container_name,
                )

            # Delete the per-host btrfs subvolume before the named volume. The
            # VPS-destroy that follows takes the whole loop file with it, so this is
            # primarily belt-and-suspenders for a destroy retried on a still-existing
            # VPS. ``delete_btrfs_subvolume_on_outer`` already no-ops on an absent
            # subvolume, so any raised error means a present subvolume remains.
            subvolume_path = self.config.btrfs_mount_path / host_id.get_uuid().hex
            _record_cleanup_attempt(
                failures,
                host_id,
                lambda: delete_btrfs_subvolume_on_outer(outer, subvolume_path),
                action="delete btrfs subvolume",
                resource=str(subvolume_path),
            )

            # Remove the unified host volume (the named entry; the data lived on the
            # subvolume above). ``docker volume rm -f`` no-ops on a missing volume,
            # so any raised error means the named volume entry remains.
            volume_name = handle.volume_name
            if volume_name is not None:
                _record_cleanup_attempt(
                    failures,
                    host_id,
                    lambda: remove_volume(outer, volume_name),
                    action="remove host volume",
                    resource=volume_name,
                )

            # Remove the per-host snapshot-trigger volume (the named entry; the shared
            # bind source at OUTER_SNAPSHOT_TRIGGER_DIR is left alone). Same ``-f``
            # no-op-on-missing semantics as the host volume above.
            trigger_volume_name = snapshot_trigger_volume_name_for(host_id)
            _record_cleanup_attempt(
                failures,
                host_id,
                lambda: remove_volume(outer, trigger_volume_name),
                action="remove snapshot trigger volume",
                resource=trigger_volume_name,
            )

    def snapshot_placement(self, outer: OuterHostInterface, host_id: HostId, handle: PlacementHandle) -> SnapshotId:
        image_tag = f"mngr-snapshot-{host_id.get_uuid().hex}-{int(time.time())}"
        with log_span("Creating Docker snapshot"):
            image_id = commit_container(outer, self._require_container_name(handle), image_tag)
        return SnapshotId(image_id)

    def delete_snapshot_placement(self, outer: OuterHostInterface, snapshot_id: SnapshotId) -> None:
        # Idempotent: an already-absent image ("No such image", a stable docker string)
        # means the postcondition -- the snapshot is gone -- already holds, so treat it
        # as success. Any OTHER rmi failure means the image still exists, so raise rather
        # than swallow: the provider's delete_snapshot only drops the snapshot from the
        # host record on success, and a failed delete must not be reported as one.
        # (The original docker provider only logs a warning here; this one raises.)
        try:
            run_docker(outer, ["rmi", str(snapshot_id)])
        except MngrError as e:
            if "no such image" in str(e).lower():
                logger.trace("Snapshot image {} already gone -- nothing to remove", snapshot_id)
                return
            raise
