import json
import shutil
import socket
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from functools import cached_property
from pathlib import Path
from typing import Any
from typing import Iterator
from typing import Mapping
from typing import Sequence

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.local.volume import LocalVolume
from imbue.mngr.providers.ssh_host_setup import build_add_authorized_keys_command
from imbue.mngr.providers.ssh_host_setup import build_add_known_hosts_command
from imbue.mngr.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import format_as_known_hosts_address
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.constants import CLOUD_INIT_TIMEOUT_SECONDS
from imbue.mngr_lima.constants import lima_host_data_disk_mount_path
from imbue.mngr_lima.constants import lima_host_data_disk_name
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.errors import LimaHostCreationError
from imbue.mngr_lima.errors import LimaHostRenameError
from imbue.mngr_lima.host_store import HostRecord
from imbue.mngr_lima.host_store import LimaHostConfig
from imbue.mngr_lima.host_store import LimaHostStore
from imbue.mngr_lima.lima_yaml import generate_default_lima_yaml
from imbue.mngr_lima.lima_yaml import load_user_lima_yaml
from imbue.mngr_lima.lima_yaml import merge_lima_yaml
from imbue.mngr_lima.lima_yaml import parse_build_args_for_yaml_path
from imbue.mngr_lima.lima_yaml import write_lima_yaml
from imbue.mngr_lima.limactl import LimaSshConfig
from imbue.mngr_lima.limactl import lima_instance_name
from imbue.mngr_lima.limactl import limactl_delete
from imbue.mngr_lima.limactl import limactl_disk_create
from imbue.mngr_lima.limactl import limactl_disk_delete
from imbue.mngr_lima.limactl import limactl_list
from imbue.mngr_lima.limactl import limactl_shell
from imbue.mngr_lima.limactl import limactl_show_ssh
from imbue.mngr_lima.limactl import limactl_start_existing
from imbue.mngr_lima.limactl import limactl_start_new
from imbue.mngr_lima.limactl import limactl_stop
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
from imbue.mngr_vps_docker.container_setup import create_bind_volume_on_outer
from imbue.mngr_vps_docker.container_setup import ensure_btrfs_subvolume_on_outer
from imbue.mngr_vps_docker.container_setup import exec_in_container
from imbue.mngr_vps_docker.container_setup import host_volume_name_for
from imbue.mngr_vps_docker.container_setup import provision_snapshot_helper_on_outer
from imbue.mngr_vps_docker.container_setup import pull_image
from imbue.mngr_vps_docker.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps_docker.container_setup import run_container
from imbue.mngr_vps_docker.container_setup import seed_host_volume_layout_on_outer
from imbue.mngr_vps_docker.container_setup import setup_container_ssh
from imbue.mngr_vps_docker.container_setup import snapshot_trigger_volume_name_for
from imbue.mngr_vps_docker.container_setup import start_container
from imbue.mngr_vps_docker.container_setup import start_container_sshd

# Lima instance status values mapped to mngr HostState
_LIMA_STATUS_TO_HOST_STATE: dict[str, HostState] = {
    "Running": HostState.RUNNING,
    "Stopped": HostState.STOPPED,
    "Broken": HostState.CRASHED,
    "Unknown": HostState.CRASHED,
}

# Filename of the pre-injected ed25519 sshd host key stored per host on disk.
_HOST_KEY_NAME = "ssh_host_ed25519_key"


class LimaProviderInstance(BaseProviderInstance):
    """Provider instance for managing Lima VMs as hosts.

    Each VM runs Lima's default user (matching the host username) with
    passwordless sudo. SSH access is managed entirely by Lima. The provider
    uses a local volume directory for persistent host state.
    """

    config: LimaProviderConfig = Field(frozen=True, description="Lima provider configuration")

    _lima_checked: bool = PrivateAttr(default=False)

    def _ensure_lima_available(self) -> None:
        """Lazily check that limactl is installed and meets version requirements.

        Called on first operation that needs limactl. Raises ProviderUnavailableError
        if limactl is not installed or is too old. This deferred check allows the
        provider to be registered without limactl being present (e.g. in CI).
        """
        if self._lima_checked:
            return
        from imbue.mngr_lima.limactl import check_lima_installed
        from imbue.mngr_lima.limactl import check_lima_version

        check_lima_installed(self.name)
        check_lima_version(self.mngr_ctx.concurrency_group, self.name, self.config.minimum_lima_version)
        self._lima_checked = True

    @property
    def supports_snapshots(self) -> bool:
        return False

    @property
    def supports_shutdown_hosts(self) -> bool:
        return True

    @property
    def supports_volumes(self) -> bool:
        return True

    @property
    def supports_mutable_tags(self) -> bool:
        return True

    def reset_caches(self) -> None:
        for host_id in list(self._host_by_id_cache):
            self._evict_cached_host(host_id)
        self._host_store.clear_cache()

    # =========================================================================
    # Directory and Store Properties
    # =========================================================================

    @property
    def _provider_dir(self) -> Path:
        """Base directory for Lima provider state: ~/.mngr/providers/lima/"""
        return self.mngr_ctx.profile_dir / "providers" / "lima" / str(self.name)

    @property
    def _volumes_dir(self) -> Path:
        """Directory containing per-host volume directories."""
        return self._provider_dir / "volumes"

    @property
    def _keys_dir(self) -> Path:
        """Directory for SSH keys."""
        return self._provider_dir / "keys"

    def _host_keys_dir(self, host_id: HostId) -> Path:
        """Directory holding this host's pre-injected sshd host keypair and matching known_hosts file."""
        return self._keys_dir / "hosts" / str(host_id)

    def _host_keypair_paths(self, host_id: HostId) -> tuple[Path, Path]:
        """Return (private_key_path, public_key_path) for this host's pre-injected sshd host key."""
        host_keys_dir = self._host_keys_dir(host_id)
        return host_keys_dir / _HOST_KEY_NAME, host_keys_dir / f"{_HOST_KEY_NAME}.pub"

    def _host_known_hosts_path(self, host_id: HostId) -> Path:
        """Path to this host's per-host known_hosts file, under its keys dir."""
        return self._host_keys_dir(host_id) / "known_hosts"

    def _ensure_host_keypair(self, host_id: HostId) -> tuple[str, str]:
        """Generate (or load) this host's ed25519 keypair, returning ``(private_key_pem, public_key_openssh)``."""
        private_key_path, public_key_openssh = load_or_create_host_keypair(
            self._host_keys_dir(host_id), _HOST_KEY_NAME
        )
        return private_key_path.read_text(), public_key_openssh

    @property
    def _tags_dir(self) -> Path:
        """Directory for per-host tag files."""
        return self._provider_dir / "tags"

    @cached_property
    def _state_volume(self) -> LocalVolume:
        """Volume for host records (provider-wide)."""
        state_dir = self._provider_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return LocalVolume(root_path=state_dir)

    @cached_property
    def _host_store(self) -> LimaHostStore:
        """Host record store backed by the state volume."""
        return LimaHostStore(volume=self._state_volume)

    # =========================================================================
    # Volume Helpers
    # =========================================================================

    def _ensure_host_volume_dir(self, host_id: HostId) -> Path:
        """Create and return the per-host volume directory."""
        volume_dir = self._volumes_dir / str(host_id)
        volume_dir.mkdir(parents=True, exist_ok=True)
        return volume_dir

    def _get_host_volume_dir(self, host_id: HostId) -> Path:
        """Get the per-host volume directory (may not exist)."""
        return self._volumes_dir / str(host_id)

    def _volume_id_for_host(self, host_id: HostId) -> VolumeId:
        """Generate a deterministic volume ID for a host."""
        return VolumeId(f"vol-{host_id.get_uuid().hex}")

    # =========================================================================
    # Tag Helpers
    # =========================================================================

    def _tags_path(self, host_id: HostId) -> Path:
        """Path to the JSON file storing tags for a host."""
        return self._tags_dir / f"{host_id}.json"

    def _read_tags(self, host_id: HostId) -> dict[str, str]:
        """Read tags from the per-host JSON file."""
        path = self._tags_path(host_id)
        if not path.exists():
            return {}
        try:
            return dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, ValueError):
            logger.warning("Invalid tags file for host {}", host_id)
            return {}

    def _write_tags(self, host_id: HostId, tags: dict[str, str]) -> None:
        """Write tags to the per-host JSON file."""
        path = self._tags_path(host_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tags, indent=2))

    # =========================================================================
    # SSH and Host Object Helpers
    # =========================================================================

    def _get_ssh_config(self, instance_name: str) -> LimaSshConfig:
        """Get SSH connection info from Lima."""
        return limactl_show_ssh(self.mngr_ctx.concurrency_group, instance_name)

    def _create_host_object(
        self,
        host_id: HostId,
        host_name: HostName,
        ssh_config: LimaSshConfig,
    ) -> Host:
        """Create a Host object from SSH connection info."""
        # Add the host to known_hosts. Re-run on every create/start/get_host
        # because Lima reassigns the forwarded port across restarts.
        self._record_pre_injected_host_key(host_id, ssh_config.hostname, ssh_config.port)

        pyinfra_host = create_pyinfra_host(
            hostname=ssh_config.hostname,
            port=ssh_config.port,
            private_key_path=ssh_config.identity_file,
            known_hosts_path=self._host_known_hosts_path(host_id),
            ssh_user=ssh_config.user,
        )
        connector = PyinfraConnector(pyinfra_host)

        return Host(
            id=host_id,
            host_name=host_name,
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

    def _record_pre_injected_host_key(self, host_id: HostId, hostname: str, port: int) -> None:
        """Write this host's known_hosts file from its pre-injected public key.

        Uses atomic_write so a concurrent reader never sees a partial file.
        """
        _, public_key_path = self._host_keypair_paths(host_id)
        public_key = public_key_path.read_text().strip()
        host_pattern = format_as_known_hosts_address(hostname, port)
        atomic_write(self._host_known_hosts_path(host_id), f"{host_pattern} {public_key}\n")

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData) -> None:
        """Update the certified host data in the host record."""
        with log_span("Updating certified host data", host_id=str(host_id)):
            host_record = self._host_store.read_host_record(host_id, use_cache=False)
            if host_record is None:
                raise HostNotFoundError(self.name, host_id)
            updated_host_record = host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, certified_data),
            )
            self._host_store.write_host_record(updated_host_record)

    def _create_offline_host(self, host_record: HostRecord) -> OfflineHost:
        """Create an OfflineHost from a host record."""
        host_id = HostId(host_record.certified_host_data.host_id)
        return OfflineHost(
            id=host_id,
            certified_host_data=host_record.certified_host_data,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

    def _create_shutdown_script(self, host: Host) -> None:
        """Create the shutdown.sh script inside the VM.

        For Lima, the shutdown script calls sudo poweroff.
        """
        host_dir_str = str(host.host_dir)

        script_content = f"""#!/bin/bash
# Auto-generated shutdown script for mngr Lima host
# Calls sudo poweroff to stop the VM

LOG_FILE="{host_dir_str}/logs/shutdown.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
    echo "$*"
}}

log "=== Shutdown script started ==="
log "STOP_REASON: ${{1:-PAUSED}}"

sudo poweroff
"""

        commands_dir = host.host_dir / "commands"
        script_path = commands_dir / "shutdown.sh"

        with log_span("Creating shutdown script at {}", script_path):
            host.write_text_file(script_path, script_content, mode="755")

    def _save_failed_host_record(
        self,
        host_id: HostId,
        host_name: HostName,
        tags: Mapping[str, str] | None,
        failure_reason: str,
        build_log: str,
    ) -> None:
        """Save a host record for a host that failed during creation."""
        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(host_name),
            user_tags=dict(tags) if tags else {},
            snapshots=[],
            failure_reason=failure_reason,
            build_log=build_log,
            created_at=now,
            updated_at=now,
        )
        host_record = HostRecord(certified_host_data=host_data)
        with log_span("Saving failed host record for host_id={}", host_id):
            self._host_store.write_host_record(host_record)

    def _cleanup_failed_lima_instance(
        self,
        *,
        instance_name: str,
        host_data_disk_name: str | None,
        container_host_port: int | None,
    ) -> None:
        """Best-effort teardown of a half-created Lima VM, its btrfs disk, and forwarded-port known_hosts entry.

        Tolerates already-absent resources so it is safe to call from a `finally`
        on any failure path. Also swallows concurrency-group ``ProcessError``s
        (e.g. a limactl timeout) so a slow cleanup never masks the original
        creation failure that triggered it.
        """
        try:
            limactl_delete(self.mngr_ctx.concurrency_group, instance_name, force=True)
        except (LimaCommandError, OSError, ProcessError) as cleanup_err:
            logger.debug("Failed to clean up Lima instance {} during error recovery: {}", instance_name, cleanup_err)
        if host_data_disk_name is not None:
            try:
                limactl_disk_delete(self.mngr_ctx.concurrency_group, host_data_disk_name, force=True)
            except (LimaCommandError, OSError, ProcessError) as cleanup_err:
                logger.debug(
                    "Failed to clean up Lima disk {} during error recovery: {}", host_data_disk_name, cleanup_err
                )
        if container_host_port is not None:
            try:
                remove_host_from_known_hosts(self._container_known_hosts_path(), "127.0.0.1", container_host_port)
            except OSError as cleanup_err:
                logger.trace("Failed to clean up container known_hosts during error recovery: {}", cleanup_err)

    def _wait_for_cloud_init(self, instance_name: str) -> None:
        """Wait for cloud-init to complete inside the VM."""
        with log_span("Waiting for cloud-init to complete in {}", instance_name):
            exit_code, stdout, stderr = limactl_shell(
                self.mngr_ctx.concurrency_group,
                instance_name,
                "cloud-init status --wait 2>/dev/null || true",
                timeout=CLOUD_INIT_TIMEOUT_SECONDS,
            )
            if exit_code != 0:
                logger.debug("cloud-init wait returned non-zero (may not be installed): {}", stderr)

    # =========================================================================
    # Docker-in-VM helpers (is_host_in_docker mode)
    # =========================================================================

    def _ensure_keys_dir(self) -> Path:
        """Create (if needed) and return the provider-wide keys directory."""
        self._keys_dir.mkdir(parents=True, exist_ok=True)
        return self._keys_dir

    def _outer_ssh_keypair(self) -> tuple[Path, str]:
        """Client keypair mngr uses to reach the VM as root (the outer)."""
        return load_or_create_ssh_keypair(self._ensure_keys_dir(), "outer_ssh_key")

    def _container_ssh_keypair(self) -> tuple[Path, str]:
        """Client keypair mngr uses to reach the agent container (the host)."""
        return load_or_create_ssh_keypair(self._ensure_keys_dir(), "container_ssh_key")

    def _container_host_keypair(self) -> tuple[Path, str]:
        """Ed25519 host keypair installed as the container sshd's host key."""
        return load_or_create_host_keypair(self._ensure_keys_dir(), "container_host_key")

    def _container_known_hosts_path(self) -> Path:
        return self._ensure_keys_dir() / "container_known_hosts"

    @contextmanager
    def _make_outer_for_vm(self, host_id: HostId, ssh_config: LimaSshConfig) -> Iterator[OuterHostInterface]:
        """Open a root outer host on the VM so we can drive docker / btrfs / systemctl."""
        # Re-run on every open because Lima reassigns the forwarded port across restarts.
        self._record_pre_injected_host_key(host_id, ssh_config.hostname, ssh_config.port)
        outer_key_path, _outer_public_key = self._outer_ssh_keypair()
        pyinfra_host = create_pyinfra_host(
            hostname=ssh_config.hostname,
            port=ssh_config.port,
            private_key_path=outer_key_path,
            known_hosts_path=self._host_known_hosts_path(host_id),
            ssh_user="root",
        )
        outer = OuterHost(id=HostId.generate(), connector=PyinfraConnector(pyinfra_host), mngr_ctx=self.mngr_ctx)
        try:
            yield outer
        finally:
            outer.disconnect()

    def _create_container_host_object(self, host_id: HostId, host_name: HostName, container_host_port: int) -> Host:
        """Create a Host object with direct SSH to the agent container via the Lima-forwarded port."""
        container_key_path, _container_public_key = self._container_ssh_keypair()
        pyinfra_host = create_pyinfra_host(
            hostname="127.0.0.1",
            port=container_host_port,
            private_key_path=container_key_path,
            known_hosts_path=self._container_known_hosts_path(),
            ssh_user="root",
        )
        connector = PyinfraConnector(pyinfra_host)
        return Host(
            id=host_id,
            host_name=host_name,
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

    def _create_docker_shutdown_script(self, host: Host) -> None:
        """Write the shutdown script that stops the agent container on idle (kill -TERM 1)."""
        shutdown_script = "#!/bin/bash\nkill -TERM 1\n"
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", shutdown_script.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    def _create_docker_host(
        self,
        name: HostName,
        image: ImageReference | None,
        tags: Mapping[str, str] | None,
        build_args: Sequence[str] | None,
        start_args: Sequence[str] | None,
        lifecycle: HostLifecycleOptions | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
    ) -> Host:
        """Create a Lima VM that runs the agent inside a Docker container (is_host_in_docker mode)."""
        if self.config.is_host_data_volume_exposed:
            raise LimaHostCreationError(
                self.name,
                "is_host_in_docker=True requires the btrfs additional-disk layout; set "
                "providers.lima.is_host_data_volume_exposed=false.",
            )
        host_id = HostId.generate()
        instance_name = lima_instance_name(name, self.mngr_ctx.config.prefix)
        logger.info("Creating Lima docker host {} ({}) ...", name, instance_name)

        host_data_disk_name = lima_host_data_disk_name(host_id)
        container_host_port = _allocate_free_host_port()
        container_ssh_port = self.config.container_ssh_port

        host_private_key_pem, host_public_key_openssh = self._ensure_host_keypair(host_id)
        _outer_key_path, outer_public_key = self._outer_ssh_keypair()
        # Materialize the container keypairs now so they exist when we set up the container later.
        self._container_ssh_keypair()
        self._container_host_keypair()

        lima_config = generate_default_lima_yaml(
            volume_host_path=None,
            host_dir=str(self.host_dir),
            custom_image_url=str(image) if image else None,
            config_image_url_aarch64=self.config.default_image_url_aarch64,
            config_image_url_x86_64=self.config.default_image_url_x86_64,
            host_private_key_pem=host_private_key_pem,
            host_public_key_openssh=host_public_key_openssh,
            host_data_disk_name=host_data_disk_name,
            host_data_disk_size=self.config.host_data_disk_size,
            is_docker_mode=True,
            outer_authorized_public_key=outer_public_key,
            container_forward_guest_port=container_ssh_port,
            container_forward_host_port=container_host_port,
            install_gvisor_runtime=self.config.install_gvisor_runtime,
        )
        yaml_path = write_lima_yaml(lima_config)
        effective_start_args = tuple(self.config.default_start_args) + tuple(start_args or ())

        # Tracked so the `finally` can tear down a half-built VM + disk on ANY
        # failure -- including ConcurrencyExceptionGroup / ProcessTimeoutError /
        # KeyboardInterrupt, which are not MngrError/OSError and would otherwise
        # escape the `except` below leaving an orphaned, untracked VM behind.
        is_creation_successful = False
        failure_reason = "Lima docker host creation was interrupted by an unexpected error"
        try:
            limactl_disk_create(self.mngr_ctx.concurrency_group, host_data_disk_name, self.config.host_data_disk_size)
            limactl_start_new(
                self.mngr_ctx.concurrency_group,
                instance_name,
                yaml_path,
                start_args=effective_start_args,
                timeout=self.config.vm_start_timeout_seconds,
            )
            self._wait_for_cloud_init(instance_name)
            ssh_config = self._get_ssh_config(instance_name)
            with log_span("Waiting for VM SSH to be ready..."):
                wait_for_sshd(ssh_config.hostname, ssh_config.port, self.config.ssh_connect_timeout)

            with self._make_outer_for_vm(host_id, ssh_config) as outer:
                base_image, container_name = self._provision_docker_container(
                    outer=outer,
                    host_id=host_id,
                    name=name,
                    host_data_disk_name=host_data_disk_name,
                    base_image=str(image) if image else self.config.default_image,
                    build_args=tuple(build_args or ()),
                    container_ssh_port=container_ssh_port,
                    container_host_port=container_host_port,
                    tags=tags,
                    known_hosts=known_hosts,
                    authorized_keys=authorized_keys,
                )
                host = self._finalize_docker_host(
                    outer=outer,
                    host_id=host_id,
                    name=name,
                    instance_name=instance_name,
                    container_name=container_name,
                    container_host_port=container_host_port,
                    base_image=base_image,
                    host_data_disk_name=host_data_disk_name,
                    effective_start_args=effective_start_args,
                    image=image,
                    lifecycle=lifecycle,
                    tags=tags,
                    lima_config=lima_config,
                )
            is_creation_successful = True
        except (MngrError, OSError) as e:
            failure_reason = str(e)
            raise LimaHostCreationError(self.name, failure_reason) from e
        finally:
            yaml_path.unlink(missing_ok=True)
            if not is_creation_successful:
                logger.error("Lima docker host creation failed; tearing down {}: {}", instance_name, failure_reason)
                self._cleanup_failed_lima_instance(
                    instance_name=instance_name,
                    host_data_disk_name=host_data_disk_name,
                    container_host_port=container_host_port,
                )
                try:
                    self._save_failed_host_record(
                        host_id=host_id, host_name=name, tags=tags, failure_reason=failure_reason, build_log=""
                    )
                except (MngrError, OSError) as record_err:
                    logger.warning("Failed to write failed-host record for {}: {}", host_id, record_err)

        self._evict_cached_host(host_id, replacement=host)
        return host

    def _provision_docker_container(
        self,
        *,
        outer: OuterHostInterface,
        host_id: HostId,
        name: HostName,
        host_data_disk_name: str,
        base_image: str,
        build_args: tuple[str, ...],
        container_ssh_port: int,
        container_host_port: int,
        tags: Mapping[str, str] | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
    ) -> tuple[str, str]:
        """Set up the btrfs subvolume, snapshot helper, image, and agent container in the VM.

        Returns ``(resolved_image, container_name)``.
        """
        btrfs_mount = Path(lima_host_data_disk_mount_path(host_data_disk_name))
        subvolume_path = btrfs_mount / host_id.get_uuid().hex
        volume_name = host_volume_name_for(host_id)
        trigger_volume_name = snapshot_trigger_volume_name_for(host_id)

        with log_span("Provisioning host volume on btrfs subvolume in VM"):
            ensure_btrfs_subvolume_on_outer(outer, subvolume_path)
            seed_host_volume_layout_on_outer(outer, subvolume_path)
            create_bind_volume_on_outer(outer, volume_name=volume_name, device_path=subvolume_path)

        provision_snapshot_helper_on_outer(
            outer,
            self.mngr_ctx.concurrency_group,
            host_id=host_id,
            btrfs_mount_path=btrfs_mount,
            subvolume_path=subvolume_path,
            trigger_volume_name=trigger_volume_name,
        )

        if build_args:
            resolved_image = build_image_on_outer_from_build_args(
                outer,
                self.mngr_ctx.concurrency_group,
                host_id=host_id,
                docker_build_args=build_args,
                git_depth=None,
                builder=self.config.builder,
                build_timeout_seconds=self.config.image_build_timeout_seconds,
            )
        else:
            with log_span("Pulling container image {} in VM", base_image):
                pull_image(outer, base_image, timeout_seconds=self.config.docker_install_timeout)
            resolved_image = base_image

        container_name = f"{self.mngr_ctx.config.prefix}{name}"
        labels = {
            LABEL_HOST_ID: str(host_id),
            LABEL_HOST_NAME: str(name),
            LABEL_PROVIDER: str(self.name),
            LABEL_TAGS: json.dumps(dict(tags) if tags else {}),
        }
        snapshots_dir = btrfs_mount / "snapshots"
        with log_span("Starting agent container in VM"):
            run_container(
                outer,
                image=resolved_image,
                name=container_name,
                # Publish the container's sshd onto the VM's loopback; Lima forwards
                # that guest port out to 127.0.0.1:container_host_port on the host.
                port_mappings={f"127.0.0.1:{container_ssh_port}": "22"},
                volumes=[
                    f"{volume_name}:{HOST_VOLUME_MOUNT_PATH}:rw",
                    f"{trigger_volume_name}:{SNAPSHOT_TRIGGER_MOUNT_PATH}:rw",
                    f"{snapshots_dir}:{SNAPSHOT_READ_MOUNT_PATH}:ro",
                ],
                labels=labels,
                # Select a non-default container runtime (e.g. 'runsc' for gVisor) when
                # configured, plus any further `docker run` flags that runtime needs
                # (e.g. '--workdir=/' + '--security-opt=no-new-privileges' under runsc),
                # which can't otherwise be injected from config. See
                # `_build_agent_container_extra_args`.
                extra_args=_build_agent_container_extra_args(
                    self.config.docker_runtime,
                    self.config.default_container_run_args,
                ),
                entrypoint_cmd=CONTAINER_ENTRYPOINT_CMD,
            )

        _container_key_path, container_public_key = self._container_ssh_keypair()
        container_host_key_path, container_host_public_key = self._container_host_keypair()
        with log_span("Setting up SSH in container"):
            setup_container_ssh(
                outer,
                container_name,
                mngr_host_dir=str(self.host_dir),
                host_volume_mount_path=f"{HOST_VOLUME_MOUNT_PATH}/{HOST_DIR_SUBPATH}",
                container_public_key=container_public_key,
                container_host_private_key=container_host_key_path.read_text(),
                container_host_public_key=container_host_public_key,
                known_hosts_entries=tuple(known_hosts or ()),
                authorized_keys_entries=tuple(authorized_keys or ()),
            )
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname="127.0.0.1",
            port=container_host_port,
            public_key=container_host_public_key,
        )
        with log_span("Waiting for container SSH to be ready..."):
            wait_for_sshd("127.0.0.1", container_host_port, self.config.container_ssh_connect_timeout)

        return resolved_image, container_name

    def _finalize_docker_host(
        self,
        *,
        outer: OuterHostInterface,
        host_id: HostId,
        name: HostName,
        instance_name: str,
        container_name: str,
        container_host_port: int,
        base_image: str,
        host_data_disk_name: str,
        effective_start_args: tuple[str, ...],
        image: ImageReference | None,
        lifecycle: HostLifecycleOptions | None,
        tags: Mapping[str, str] | None,
        lima_config: dict[str, Any],
    ) -> Host:
        """Build the container Host object, configure activity watching, and persist the host record."""
        host = self._create_container_host_object(host_id, name, container_host_port)

        lifecycle_options = lifecycle if lifecycle is not None else HostLifecycleOptions()
        activity_config = lifecycle_options.to_activity_config(
            default_idle_timeout_seconds=self.config.default_idle_timeout,
            default_idle_mode=self.config.default_idle_mode,
            default_activity_sources=self.config.default_activity_sources,
        )
        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            idle_timeout_seconds=activity_config.idle_timeout_seconds,
            activity_sources=activity_config.activity_sources,
            host_id=str(host_id),
            host_name=str(name),
            image=base_image,
            user_tags=dict(tags) if tags else {},
            snapshots=[],
            tmux_session_prefix=self.mngr_ctx.config.prefix,
            created_at=now,
            updated_at=now,
        )
        # Persist the host record *before* set_certified_data: that call fires
        # the on_updated_host_data callback, which reads the record back from
        # the store (and raises HostNotFoundError if it isn't there yet).
        lima_config_record = LimaHostConfig(
            instance_name=instance_name,
            start_args=effective_start_args,
            image_url=str(image) if image else None,
            is_host_data_volume_exposed=False,
            host_data_disk_name=host_data_disk_name,
            is_host_in_docker=True,
            container_name=container_name,
            container_host_port=container_host_port,
            base_image=base_image,
        )
        resources = self._read_resources_from_config(lima_config)
        host_record = HostRecord(
            certified_host_data=host_data,
            ssh_hostname="127.0.0.1",
            ssh_port=container_host_port,
            ssh_user="root",
            ssh_identity_file=str(self._container_ssh_keypair()[0]),
            config=lima_config_record,
            resources=resources,
        )
        self._host_store.write_host_record(host_record)

        host.record_activity(ActivitySource.BOOT)
        host.set_certified_data(host_data)

        self._create_docker_shutdown_script(host)
        with log_span("Starting activity watcher in container"):
            exec_in_container(outer, container_name, build_start_activity_watcher_command(str(self.host_dir)))
        if tags:
            self._write_tags(host_id, dict(tags))
        return host

    def _start_docker_host(self, host_record: HostRecord) -> Host:
        """Start a stopped docker-mode host: boot the VM, then relaunch the container."""
        config = host_record.config
        host_id = HostId(host_record.certified_host_data.host_id)
        if config is None or config.container_name is None or config.container_host_port is None:
            raise MngrError(
                f"Host {host_id} is missing docker-mode configuration "
                "(container_name/container_host_port) and cannot be started."
            )
        container_host_port = config.container_host_port

        try:
            limactl_start_existing(self.mngr_ctx.concurrency_group, config.instance_name)
        except LimaCommandError as e:
            raise MngrError(f"Failed to start Lima VM {host_id}: {e}") from e

        ssh_config = self._get_ssh_config(config.instance_name)
        with log_span("Waiting for VM SSH to be ready..."):
            wait_for_sshd(ssh_config.hostname, ssh_config.port, self.config.ssh_connect_timeout)

        with self._make_outer_for_vm(host_id, ssh_config) as outer:
            with log_span("Starting agent container in VM"):
                start_container(outer, config.container_name)
            # sshd was launched via `docker exec` at create time and does not
            # survive the container going down with the VM, so relaunch it.
            start_container_sshd(outer, config.container_name)
            with log_span("Waiting for container SSH to be ready..."):
                wait_for_sshd("127.0.0.1", container_host_port, self.config.container_ssh_connect_timeout)
            host_obj = self._create_container_host_object(
                host_id, HostName(host_record.certified_host_data.host_name), container_host_port
            )
            host_obj.record_activity(ActivitySource.BOOT)
            with log_span("Restarting activity watcher in container"):
                exec_in_container(
                    outer, config.container_name, build_start_activity_watcher_command(str(self.host_dir))
                )

        updated_certified = host_record.certified_host_data.model_copy_update(
            to_update(host_record.certified_host_data.field_ref().stop_reason, None),
        )
        self._host_store.write_host_record(
            host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, updated_certified),
            )
        )
        self._evict_cached_host(host_id, replacement=host_obj)
        return host_obj

    # =========================================================================
    # Core Lifecycle Methods
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
        """Create a new Lima VM host."""
        self._ensure_lima_available()
        if self.config.is_host_in_docker:
            return self._create_docker_host(
                name=name,
                image=image,
                tags=tags,
                build_args=build_args,
                start_args=start_args,
                lifecycle=lifecycle,
                known_hosts=known_hosts,
                authorized_keys=authorized_keys,
            )
        host_id = HostId.generate()
        instance_name = lima_instance_name(name, self.mngr_ctx.config.prefix)
        logger.info("Creating Lima VM host {} ({}) ...", name, instance_name)

        # Resolve the host_dir layout once and lock it in on the host record.
        # is_host_data_volume_exposed=True (default) keeps the historical 9p
        # bind-mount layout. False switches to an in-VM btrfs additionalDisk
        # and omits the bind mount entirely.
        is_host_data_volume_exposed = self.config.is_host_data_volume_exposed
        host_data_disk_name = None if is_host_data_volume_exposed else lima_host_data_disk_name(host_id)

        # Create the persistent volume directory only in bind-mount mode; the
        # btrfs path has no host-side directory to expose.
        volume_dir: Path | None
        if is_host_data_volume_exposed:
            volume_dir = self._ensure_host_volume_dir(host_id)
        else:
            volume_dir = None

        # Generate the sshd host keypair to inject into the VM and record in known_hosts.
        host_private_key_pem, host_public_key_openssh = self._ensure_host_keypair(host_id)

        # Generate or load Lima YAML config
        yaml_path_from_build_args = parse_build_args_for_yaml_path(tuple(build_args or ()))
        if yaml_path_from_build_args is not None:
            user_config = load_user_lima_yaml(yaml_path_from_build_args)
            base_config = generate_default_lima_yaml(
                volume_host_path=volume_dir,
                host_dir=str(self.host_dir),
                config_image_url_aarch64=self.config.default_image_url_aarch64,
                config_image_url_x86_64=self.config.default_image_url_x86_64,
                host_private_key_pem=host_private_key_pem,
                host_public_key_openssh=host_public_key_openssh,
                host_data_disk_name=host_data_disk_name,
                host_data_disk_size=self.config.host_data_disk_size if host_data_disk_name else None,
            )
            lima_config = merge_lima_yaml(base_config, user_config)
        else:
            image_url = str(image) if image else None
            lima_config = generate_default_lima_yaml(
                volume_host_path=volume_dir,
                host_dir=str(self.host_dir),
                custom_image_url=image_url,
                config_image_url_aarch64=self.config.default_image_url_aarch64,
                config_image_url_x86_64=self.config.default_image_url_x86_64,
                host_private_key_pem=host_private_key_pem,
                host_public_key_openssh=host_public_key_openssh,
                host_data_disk_name=host_data_disk_name,
                host_data_disk_size=self.config.host_data_disk_size if host_data_disk_name else None,
            )

        # Write the YAML config to a temp file
        yaml_path = write_lima_yaml(lima_config)

        effective_start_args = tuple(self.config.default_start_args) + tuple(start_args or ())

        # Tracked so the `finally` can tear down a half-built VM + disk on ANY
        # failure -- including ConcurrencyExceptionGroup / ProcessTimeoutError /
        # KeyboardInterrupt, which are not MngrError/OSError and would otherwise
        # escape the `except` below leaving an orphaned, untracked VM behind.
        is_creation_successful = False
        failure_reason = "Lima host creation was interrupted by an unexpected error"
        try:
            # Pre-create the Lima-managed additional disk in btrfs mode.
            # `additionalDisks` with `format: true` only auto-formats an
            # already-existing disk; without this pre-create, `limactl start`
            # fails with "could not load disk ... no such file or directory".
            if host_data_disk_name is not None:
                limactl_disk_create(
                    self.mngr_ctx.concurrency_group,
                    host_data_disk_name,
                    self.config.host_data_disk_size,
                )

            # Create and start the Lima instance
            limactl_start_new(
                self.mngr_ctx.concurrency_group,
                instance_name,
                yaml_path,
                start_args=effective_start_args,
                timeout=self.config.vm_start_timeout_seconds,
            )

            # Wait for cloud-init to complete
            self._wait_for_cloud_init(instance_name)

            # Get SSH connection info
            ssh_config = self._get_ssh_config(instance_name)

            # Wait for SSH to be ready
            with log_span("Waiting for SSH to be ready..."):
                wait_for_sshd(ssh_config.hostname, ssh_config.port, self.config.ssh_connect_timeout)

            # Create the Host object
            host = self._create_host_object(host_id, name, ssh_config)
            is_creation_successful = True

        except (MngrError, OSError) as e:
            failure_reason = str(e)
            raise LimaHostCreationError(self.name, failure_reason) from e
        finally:
            # Clean up the temporary YAML config file
            yaml_path.unlink(missing_ok=True)
            if not is_creation_successful:
                logger.error("Lima host creation failed; tearing down {}: {}", instance_name, failure_reason)
                # Tear down the VM (and the orphaned btrfs additional disk so a
                # retry with the same host_id can re-create it without colliding).
                self._cleanup_failed_lima_instance(
                    instance_name=instance_name,
                    host_data_disk_name=host_data_disk_name,
                    container_host_port=None,
                )
                try:
                    self._save_failed_host_record(
                        host_id=host_id, host_name=name, tags=tags, failure_reason=failure_reason, build_log=""
                    )
                except (MngrError, OSError) as record_err:
                    logger.warning("Failed to write failed-host record for {}: {}", host_id, record_err)

        # Build lifecycle config
        lifecycle_options = lifecycle if lifecycle is not None else HostLifecycleOptions()
        activity_config = lifecycle_options.to_activity_config(
            default_idle_timeout_seconds=self.config.default_idle_timeout,
            default_idle_mode=self.config.default_idle_mode,
            default_activity_sources=self.config.default_activity_sources,
        )

        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            idle_timeout_seconds=activity_config.idle_timeout_seconds,
            activity_sources=activity_config.activity_sources,
            host_id=str(host_id),
            host_name=str(name),
            user_tags=dict(tags) if tags else {},
            snapshots=[],
            tmux_session_prefix=self.mngr_ctx.config.prefix,
            created_at=now,
            updated_at=now,
        )

        # Build and save host record with resources
        lima_config_record = LimaHostConfig(
            instance_name=instance_name,
            start_args=effective_start_args,
            image_url=str(image) if image else None,
            is_host_data_volume_exposed=is_host_data_volume_exposed,
            host_data_disk_name=host_data_disk_name,
        )

        # Read configured resources from Lima config
        resources = self._read_resources_from_config(lima_config)

        host_record = HostRecord(
            certified_host_data=host_data,
            ssh_hostname=ssh_config.hostname,
            ssh_port=ssh_config.port,
            ssh_user=ssh_config.user,
            ssh_identity_file=str(ssh_config.identity_file),
            config=lima_config_record,
            resources=resources,
        )
        self._host_store.write_host_record(host_record)

        # Save tags
        if tags:
            self._write_tags(host_id, dict(tags))

        # Record boot activity and set certified data
        host.record_activity(ActivitySource.BOOT)
        host.set_certified_data(host_data)

        # Install shutdown script
        self._create_shutdown_script(host)

        # Start the activity watcher
        with log_span("Starting activity watcher in VM"):
            start_activity_watcher_cmd = build_start_activity_watcher_command(str(self.host_dir))
            host.execute_stateful_command(f"sh -c '{start_activity_watcher_cmd}'")

        # Add authorized keys if provided
        if authorized_keys:
            add_authorized_keys_cmd = build_add_authorized_keys_command(ssh_config.user, tuple(authorized_keys))
            if add_authorized_keys_cmd is not None:
                with log_span("Adding {} authorized_keys entries to VM", len(authorized_keys)):
                    host.execute_stateful_command(f"sh -c '{add_authorized_keys_cmd}'")

        # Add known hosts entries if provided
        if known_hosts:
            add_known_hosts_cmd = build_add_known_hosts_command(ssh_config.user, tuple(known_hosts))
            if add_known_hosts_cmd is not None:
                with log_span("Adding {} known_hosts entries to VM", len(known_hosts)):
                    host.execute_stateful_command(f"sh -c '{add_known_hosts_cmd}'")

        self._evict_cached_host(host_id, replacement=host)
        return host

    def _read_resources_from_config(self, lima_config: dict) -> HostResources:
        """Read configured resources from a Lima YAML config dict."""
        cpus = lima_config.get("cpus", 4)
        memory_str = lima_config.get("memory", "4GiB")
        disk_str = lima_config.get("disk", "100GiB")

        # Parse memory (Lima uses strings like "4GiB")
        memory_gb = _parse_size_to_gb(memory_str) if isinstance(memory_str, str) else float(memory_str)
        disk_gb = _parse_size_to_gb(disk_str) if isinstance(disk_str, str) else float(disk_str)

        return HostResources(
            cpu=CpuResources(count=int(cpus)),
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            gpu=None,
        )

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop a Lima VM."""
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Stopping Lima VM: {}", host_id)

        # Disconnect SSH before stopping
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.config is not None:
            try:
                limactl_stop(
                    self.mngr_ctx.concurrency_group, host_record.config.instance_name, timeout=timeout_seconds
                )
            except LimaCommandError as e:
                logger.warning("Error stopping Lima VM: {}", e)
        else:
            logger.debug("No host record found for {}", host_id)

        if host_record is not None:
            updated_certified_data = host_record.certified_host_data.model_copy_update(
                to_update(host_record.certified_host_data.field_ref().stop_reason, HostState.STOPPED.value),
            )
            self._host_store.write_host_record(
                host_record.model_copy_update(
                    to_update(host_record.field_ref().certified_host_data, updated_certified_data),
                )
            )

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Start a stopped Lima VM."""
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Starting Lima VM: {}", host_id)

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)

        if host_record.config is None:
            raise MngrError(f"Host {host_id} has no configuration and cannot be started.")

        if host_record.certified_host_data.failure_reason is not None:
            raise MngrError(
                f"Host {host_id} failed during creation and cannot be started. "
                f"Reason: {host_record.certified_host_data.failure_reason}"
            )

        if host_record.config.is_host_in_docker:
            return self._start_docker_host(host_record)

        instance_name = host_record.config.instance_name

        try:
            limactl_start_existing(self.mngr_ctx.concurrency_group, instance_name)
        except LimaCommandError as e:
            raise MngrError(f"Failed to start Lima VM {host_id}: {e}") from e

        # Get SSH info and wait for connectivity
        ssh_config = self._get_ssh_config(instance_name)
        with log_span("Waiting for SSH to be ready..."):
            wait_for_sshd(ssh_config.hostname, ssh_config.port, self.config.ssh_connect_timeout)

        host_obj = self._create_host_object(host_id, HostName(host_record.certified_host_data.host_name), ssh_config)

        # Update SSH info in host record (port may change after restart)
        updated_record = host_record.model_copy_update(
            to_update(host_record.field_ref().ssh_hostname, ssh_config.hostname),
            to_update(host_record.field_ref().ssh_port, ssh_config.port),
            to_update(host_record.field_ref().ssh_user, ssh_config.user),
            to_update(host_record.field_ref().ssh_identity_file, str(ssh_config.identity_file)),
        )
        # Clear stop reason
        updated_certified = updated_record.certified_host_data.model_copy_update(
            to_update(updated_record.certified_host_data.field_ref().stop_reason, None),
        )
        updated_record = updated_record.model_copy_update(
            to_update(updated_record.field_ref().certified_host_data, updated_certified),
        )
        self._host_store.write_host_record(updated_record)

        host_obj.record_activity(ActivitySource.BOOT)

        # Restart activity watcher
        with log_span("Restarting activity watcher in VM"):
            start_activity_watcher_cmd = build_start_activity_watcher_command(str(self.host_dir))
            host_obj.execute_stateful_command(f"sh -c '{start_activity_watcher_cmd}'")

        self._evict_cached_host(host_id, replacement=host_obj)
        return host_obj

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Permanently destroy a Lima VM and mark the host as DESTROYED."""
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Destroying Lima VM: {}", host_id)

        # Disconnect SSH
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.config is not None:
            try:
                limactl_delete(self.mngr_ctx.concurrency_group, host_record.config.instance_name, force=True)
            except LimaCommandError as e:
                logger.warning("Error deleting Lima instance: {}", e)
            # Remove the Lima-managed btrfs additional disk for hosts that
            # were created with is_host_data_volume_exposed=False. The disk
            # lives under ~/.lima/_disks/ and is otherwise orphaned because
            # `limactl delete` only removes the VM definition, not named
            # disks it referenced. Tolerate the disk already being absent.
            if host_record.config.host_data_disk_name is not None:
                try:
                    limactl_disk_delete(
                        self.mngr_ctx.concurrency_group,
                        host_record.config.host_data_disk_name,
                        force=True,
                    )
                except LimaCommandError as e:
                    logger.warning("Error deleting Lima additional disk: {}", e)

            # Drop the forwarded-port entry from the container known_hosts file
            # so a later host that happens to reuse the same host port doesn't
            # trip a host-key mismatch. The container itself is gone with the VM.
            if host_record.config.is_host_in_docker and host_record.config.container_host_port is not None:
                try:
                    remove_host_from_known_hosts(
                        self._container_known_hosts_path(), "127.0.0.1", host_record.config.container_host_port
                    )
                except OSError as e:
                    logger.trace("Failed to clean up container known_hosts: {}", e)

        # Mark as destroyed in host record
        if host_record is not None:
            updated_certified = host_record.certified_host_data.model_copy_update(
                to_update(host_record.certified_host_data.field_ref().stop_reason, HostState.DESTROYED.value),
                to_update(host_record.certified_host_data.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            self._host_store.write_host_record(
                host_record.model_copy_update(
                    to_update(host_record.field_ref().certified_host_data, updated_certified),
                )
            )

    def delete_host(self, host: HostInterface) -> None:
        """Permanently delete all records associated with a destroyed host."""
        host_id = host.id
        logger.info("Deleting Lima host records: {}", host_id)

        # If the host was created in btrfs mode and its Lima disk somehow
        # outlived destroy_host (e.g. destroy never ran or the disk-delete
        # call previously raised), clean it up as a safety net before we
        # forget about it. Tolerates "not found" via limactl_disk_delete.
        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if (
            host_record is not None
            and host_record.config is not None
            and host_record.config.host_data_disk_name is not None
        ):
            try:
                limactl_disk_delete(
                    self.mngr_ctx.concurrency_group,
                    host_record.config.host_data_disk_name,
                    force=True,
                )
            except LimaCommandError as e:
                logger.warning("Error deleting Lima additional disk during delete_host: {}", e)

        # Delete host record from store
        self._host_store.delete_host_record(host_id)

        # Delete volume directory (no-op in btrfs mode: the directory was
        # never created because is_host_data_volume_exposed=False).
        volume_dir = self._get_host_volume_dir(host_id)
        if volume_dir.exists():
            shutil.rmtree(volume_dir, ignore_errors=True)

        # Delete tags file
        tags_path = self._tags_path(host_id)
        if tags_path.exists():
            tags_path.unlink(missing_ok=True)

        # Delete the per-host keys directory (holds the pre-injected sshd
        # keypair and the matching known_hosts file).
        host_keys_dir = self._host_keys_dir(host_id)
        if host_keys_dir.exists():
            shutil.rmtree(host_keys_dir, ignore_errors=True)

        self._evict_cached_host(host_id)

    def on_connection_error(self, host_id: HostId) -> None:
        """Handle connection errors by clearing the cache."""
        self._evict_cached_host(host_id)

    # =========================================================================
    # Discovery Methods
    # =========================================================================

    def get_host(self, host: HostId | HostName) -> HostInterface:
        """Retrieve a host by ID or name."""
        if isinstance(host, HostId):
            return self._get_host_by_id(host)
        return self._get_host_by_name(host)

    def _get_host_by_id(self, host_id: HostId) -> HostInterface:
        """Get a host by ID."""
        if host_id in self._host_by_id_cache:
            return self._host_by_id_cache[host_id]

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)

        if host_record.config is None or host_record.ssh_hostname is None:
            # Failed or offline host
            return self._create_offline_host(host_record)

        # Check if the Lima instance is running
        instances = limactl_list(self.mngr_ctx.concurrency_group)
        instance_name = host_record.config.instance_name
        is_running = any(inst.get("name") == instance_name and inst.get("status") == "Running" for inst in instances)

        if not is_running:
            return self._create_offline_host(host_record)

        # Instance is running -- create online host.
        if host_record.config.is_host_in_docker:
            if host_record.config.container_host_port is None:
                raise MngrError(f"Host {host_id} is in docker mode but its record is missing container_host_port.")
            host_obj = self._create_container_host_object(
                host_id,
                HostName(host_record.certified_host_data.host_name),
                host_record.config.container_host_port,
            )
            self._evict_cached_host(host_id, replacement=host_obj)
            return host_obj
        ssh_config = self._get_ssh_config(instance_name)
        host_obj = self._create_host_object(host_id, HostName(host_record.certified_host_data.host_name), ssh_config)
        self._evict_cached_host(host_id, replacement=host_obj)
        return host_obj

    def _get_host_by_name(self, name: HostName) -> HostInterface:
        """Get a host by name."""
        # Search through host records
        for record in self._host_store.list_all_host_records():
            if record.certified_host_data.host_name == str(name):
                host_id = HostId(record.certified_host_data.host_id)
                return self._get_host_by_id(host_id)
        raise HostNotFoundError(self.name, name)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Return an offline representation of the given host."""
        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(self.name, host_id)
        return self._create_offline_host(host_record)

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        """Discover all Lima hosts managed by this provider instance.

        If limactl is not installed, returns host records from local state only
        (all marked as offline). This allows discovery to succeed gracefully
        in environments without Lima.
        """
        prefix = self.mngr_ctx.config.prefix

        # Get all Lima instances with our prefix (gracefully handle missing limactl)
        instances: list[dict[str, Any]] = []
        try:
            self._ensure_lima_available()
            instances = limactl_list(cg)
        except (LimaCommandError, OSError) as e:
            logger.warning("Failed to list Lima instances: {}", e)
        except ProviderUnavailableError as e:
            logger.debug("Lima provider not available for discovery: {}", e)

        # Build a map of instance_name -> status
        instance_status: dict[str, str] = {}
        for inst in instances:
            inst_name = inst.get("name", "")
            if inst_name.startswith(prefix):
                instance_status[inst_name] = inst.get("status", "Unknown")

        # Discover from host records (covers stopped/destroyed hosts too)
        discovered: list[DiscoveredHost] = []
        for record in self._host_store.list_all_host_records():
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)

            # Determine state
            if record.config is not None:
                lima_status = instance_status.pop(record.config.instance_name, None)
                if lima_status is not None:
                    host_state = _LIMA_STATUS_TO_HOST_STATE.get(lima_status, HostState.CRASHED)
                else:
                    # Instance not found in Lima -- derive from record
                    if record.certified_host_data.failure_reason is not None:
                        host_state = HostState.FAILED
                    elif record.certified_host_data.stop_reason == HostState.DESTROYED.value:
                        host_state = HostState.DESTROYED
                    elif record.certified_host_data.stop_reason == HostState.STOPPED.value:
                        host_state = HostState.STOPPED
                    else:
                        host_state = HostState.CRASHED
            else:
                host_state = HostState.FAILED

            if host_state == HostState.DESTROYED and not include_destroyed:
                continue

            discovered.append(
                DiscoveredHost(
                    host_id=host_id,
                    host_name=host_name,
                    provider_name=self.name,
                    host_state=host_state,
                )
            )

        # Surface orphaned Lima VMs: prefix-matched instances that no host record
        # claims (whatever is left in instance_status after the loop above popped
        # every recorded host). These are leftovers from a create that failed
        # before its record was written -- e.g. a build-time error that escaped
        # cleanup -- so they are invisible to the record-driven discovery above
        # and are never reaped by gc. Warn loudly with the manual cleanup command.
        # We deliberately do not emit synthetic DiscoveredHosts for them: gc would
        # call get_host(), which raises HostNotFoundError for an id with no record.
        for orphan_instance_name, orphan_status in instance_status.items():
            logger.warning(
                "Found orphaned Lima VM {!r} (status={}) with no mngr host record -- likely a failed or "
                "interrupted create. mngr cannot manage or garbage-collect it; remove it manually with "
                "`limactl delete --force {}`.",
                orphan_instance_name,
                orphan_status,
                orphan_instance_name,
            )

        return discovered

    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get configured resources from the persistent host record."""
        host_id = host.id
        host_record = self._host_store.read_host_record(host_id)
        if host_record is not None and host_record.resources is not None:
            return host_record.resources
        # Return defaults if no record
        return HostResources(
            cpu=CpuResources(count=4),
            memory_gb=4.0,
            disk_gb=100.0,
            gpu=None,
        )

    # =========================================================================
    # Snapshot Methods (not supported)
    # =========================================================================

    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        raise SnapshotsNotSupportedError(self.name)

    def list_snapshots(
        self,
        host: HostInterface | HostId,
    ) -> list[SnapshotInfo]:
        return []

    def delete_snapshot(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId,
    ) -> None:
        raise SnapshotsNotSupportedError(self.name)

    # =========================================================================
    # Volume Methods
    # =========================================================================

    def list_volumes(self) -> list[VolumeInfo]:
        """List all volumes managed by this provider.

        Only hosts created with is_host_data_volume_exposed=True (today's
        default) have a host-side volume directory worth listing. btrfs-mode
        hosts (is_host_data_volume_exposed=False) deliberately have no
        host-side directory, so they do not appear here even though their
        data still exists on the in-VM btrfs disk.
        """
        volumes: list[VolumeInfo] = []
        if not self._volumes_dir.exists():
            return volumes

        for volume_path in sorted(self._volumes_dir.iterdir()):
            if volume_path.is_dir():
                host_id_str = volume_path.name
                host_id = HostId(host_id_str)
                volume_id = self._volume_id_for_host(host_id)

                # Calculate total size
                total_size = sum(f.stat().st_size for f in volume_path.rglob("*") if f.is_file())

                volumes.append(
                    VolumeInfo(
                        volume_id=volume_id,
                        name=f"lima-{host_id_str}",
                        size_bytes=total_size,
                        host_id=host_id,
                        tags={},
                    )
                )

        return volumes

    def delete_volume(self, volume_id: VolumeId) -> None:
        """Delete a volume directory."""
        if not self._volumes_dir.exists():
            raise MngrError(f"Volume not found: {volume_id}")
        for volume_path in self._volumes_dir.iterdir():
            if volume_path.is_dir():
                host_id = HostId(volume_path.name)
                if self._volume_id_for_host(host_id) == volume_id:
                    shutil.rmtree(volume_path, ignore_errors=True)
                    return
        raise MngrError(f"Volume not found: {volume_id}")

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Get the host volume for a given host.

        Returns None for hosts created with is_host_data_volume_exposed=False:
        in that mode host_dir lives only on an in-VM btrfs disk, so the
        host machine has no direct read path. Callers
        (libs/mngr/imbue/mngr/api/events.py, mngr_claude's
        on_before_host_destroy hook, mngr_tmr, mngr_file) already handle
        None gracefully by skipping or falling back to online-host SSH.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._host_store.read_host_record(host_id)
        if host_record is not None and host_record.config is not None:
            if not host_record.config.is_host_data_volume_exposed:
                return None
        volume_dir = self._get_host_volume_dir(host_id)
        if not volume_dir.exists():
            return None
        volume = LocalVolume(root_path=volume_dir)
        return HostVolume(volume=volume)

    # =========================================================================
    # Host Mutation Methods
    # =========================================================================

    def get_host_tags(self, host: HostInterface | HostId) -> dict[str, str]:
        host_id = host.id if isinstance(host, HostInterface) else host
        return self._read_tags(host_id)

    def set_host_tags(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        self._write_tags(host_id, dict(tags))

    def add_tags_to_host(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        existing = self._read_tags(host_id)
        existing.update(tags)
        self._write_tags(host_id, existing)

    def remove_tags_from_host(self, host: HostInterface | HostId, keys: Sequence[str]) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        existing = self._read_tags(host_id)
        for key in keys:
            existing.pop(key, None)
        self._write_tags(host_id, existing)

    def rename_host(self, host: HostInterface | HostId, name: HostName) -> HostInterface:
        raise LimaHostRenameError()

    # =========================================================================
    # Connector Method
    # =========================================================================

    def get_connector(self, host: HostInterface | HostId) -> PyinfraHost:
        """Get the pyinfra connector for a host."""
        host_id = host.id if isinstance(host, HostInterface) else host
        host_obj = self.get_host(host_id)
        if isinstance(host_obj, Host):
            return host_obj.connector.host
        raise MngrError(f"Cannot get connector for offline host {host_id}")

    # =========================================================================
    # Agent Data Persistence
    # =========================================================================

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict[str, Any]]:
        return self._host_store.list_persisted_agent_data_for_host(host_id)

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        self._host_store.persist_agent_data(host_id, agent_data)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        self._host_store.remove_persisted_agent_data(host_id, agent_id)


def _allocate_free_host_port() -> int:
    """Return a currently-free TCP port on the host's loopback.

    Used to pick the unique host-side port Lima forwards to a docker-mode
    container's sshd. Binding to port 0 lets the OS choose a free port; we
    immediately release it and bake the number into the Lima portForwards
    config, so a brief TOCTOU window is acceptable (Lima fails loudly if the
    port is taken, and each host gets its own number).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as temp_socket:
        temp_socket.bind(("127.0.0.1", 0))
        return temp_socket.getsockname()[1]


def _build_agent_container_extra_args(
    docker_runtime: str | None,
    default_container_run_args: Sequence[str],
) -> list[str]:
    """Assemble the extra ``docker run`` args for the in-VM agent container.

    Prepends ``--runtime <value>`` when a non-default runtime is configured (e.g.
    'runsc' for gVisor), then appends any configured passthrough run args (e.g.
    '--workdir=/' + '--security-opt=no-new-privileges' the runtime needs).
    """
    runtime_args = ["--runtime", docker_runtime] if docker_runtime is not None else []
    return [*runtime_args, *default_container_run_args]


def _parse_size_to_gb(size_str: str) -> float:
    """Parse a Lima size string (e.g. '4GiB', '512MiB') to GB."""
    size_str = size_str.strip()
    if size_str.endswith("GiB"):
        return float(size_str[:-3])
    if size_str.endswith("MiB"):
        return float(size_str[:-3]) / 1024.0
    if size_str.endswith("TiB"):
        return float(size_str[:-3]) * 1024.0
    # Try plain number (assume GiB)
    try:
        return float(size_str)
    except ValueError:
        logger.warning("Could not parse size string: {}, defaulting to 4 GiB", size_str)
        return 4.0
