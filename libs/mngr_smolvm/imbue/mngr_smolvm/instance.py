import hashlib
import json
import shutil
from datetime import datetime
from datetime import timezone
from functools import cached_property
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
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
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import format_as_known_hosts_address
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_smolvm.config import SmolvmProviderConfig
from imbue.mngr_smolvm.constants import POWEROFF_SENTINEL_PATH
from imbue.mngr_smolvm.errors import SmolvmCommandError
from imbue.mngr_smolvm.errors import SmolvmHostCreationError
from imbue.mngr_smolvm.errors import SmolvmHostRenameError
from imbue.mngr_smolvm.errors import SmolvmProvisioningError
from imbue.mngr_smolvm.host_store import HostRecord
from imbue.mngr_smolvm.host_store import SmolvmHostStore
from imbue.mngr_smolvm.host_store import SmolvmMachineConfig
from imbue.mngr_smolvm.provisioning import allocate_free_tcp_port
from imbue.mngr_smolvm.provisioning import build_shutdown_script
from imbue.mngr_smolvm.provisioning import build_ssh_provisioning_script
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_create
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_delete
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_exec
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_list
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_name
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_start
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_stop
from imbue.mngr_smolvm.smolvm_cli import smolvm_pack_create_from_archive

# smolvm machine state values mapped to mngr HostState.
_SMOLVM_STATE_TO_HOST_STATE: dict[str, HostState] = {
    "running": HostState.RUNNING,
    "stopped": HostState.STOPPED,
    "created": HostState.STOPPED,
    "unreachable": HostState.CRASHED,
    "failed": HostState.FAILED,
}

# Filename of the pre-injected ed25519 sshd host key stored per host on disk.
_HOST_KEY_NAME = "ssh_host_ed25519_key"

# All SSH access goes to the forwarded localhost port.
_SSH_HOSTNAME = "127.0.0.1"

# mngr connects to smolvm guests as root: the workload container (and the
# bare-VM agent context) run as root, matching the docker provider model.
_SSH_USER = "root"


class _ParsedBuildArgs(FrozenModel):
    """Image-source selection parsed from create_host build_args."""

    image_archive: Path | None = Field(default=None, description="Path to a docker-save image archive")
    from_pack: Path | None = Field(default=None, description="Path to an existing .smolmachine sidecar")


@pure
def _parse_build_args(build_args: tuple[str, ...]) -> _ParsedBuildArgs:
    """Parse the provider build args (--image-archive PATH, --from PATH)."""
    image_archive: Path | None = None
    from_pack: Path | None = None
    idx = 0
    while idx < len(build_args):
        arg = build_args[idx]
        if arg == "--image-archive":
            if idx + 1 >= len(build_args):
                raise MngrError("--image-archive requires a PATH argument")
            image_archive = Path(build_args[idx + 1])
            idx += 2
        elif arg == "--from":
            if idx + 1 >= len(build_args):
                raise MngrError("--from requires a PATH argument")
            from_pack = Path(build_args[idx + 1])
            idx += 2
        else:
            raise MngrError(f"Unsupported smolvm build arg: {arg} (supported: --image-archive PATH, --from PATH)")
    if image_archive is not None and from_pack is not None:
        raise MngrError("--image-archive and --from are mutually exclusive")
    return _ParsedBuildArgs(image_archive=image_archive, from_pack=from_pack)


class SmolvmProviderInstance(BaseProviderInstance):
    """Provider instance for managing smolvm microVMs as hosts.

    Each machine runs as root (the workload container, or the bare-VM agent
    context when no image is configured). mngr provisions sshd inside the
    guest over the smolvm exec channel, forwards a localhost port to it, and
    accesses the host over SSH like any other remote provider. Persistent
    host state lives in a local volume directory.
    """

    config: SmolvmProviderConfig = Field(frozen=True, description="smolvm provider configuration")

    _smolvm_checked: bool = PrivateAttr(default=False)

    def _ensure_smolvm_available(self) -> None:
        """Lazily check that smolvm is installed and meets version requirements.

        Called on the first operation that needs smolvm. Raises
        ProviderUnavailableError if smolvm is not installed or is too old.
        The deferred check allows the provider to be registered without
        smolvm being present (e.g. in CI).
        """
        if self._smolvm_checked:
            return
        from imbue.mngr_smolvm.smolvm_cli import check_smolvm_installed
        from imbue.mngr_smolvm.smolvm_cli import check_smolvm_version

        check_smolvm_installed(self.name, self.config.smolvm_command)
        check_smolvm_version(
            self.mngr_ctx.concurrency_group,
            self.name,
            self.config.smolvm_command,
            self.config.minimum_smolvm_version,
        )
        self._smolvm_checked = True

    def _ensure_data_disk_support(self) -> None:
        """Check the btrfs data-disk capability (only needed in the non-exposed layout)."""
        from imbue.mngr_smolvm.smolvm_cli import check_smolvm_data_disk_support

        check_smolvm_data_disk_support(self.mngr_ctx.concurrency_group, self.name, self.config.smolvm_command)

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
        """Base directory for smolvm provider state: ~/.mngr/providers/smolvm/<name>/"""
        return self.mngr_ctx.profile_dir / "providers" / "smolvm" / str(self.name)

    @property
    def _volumes_dir(self) -> Path:
        """Directory containing per-host volume directories."""
        return self._provider_dir / "volumes"

    @property
    def _keys_dir(self) -> Path:
        """Directory for SSH keys."""
        return self._provider_dir / "keys"

    @property
    def _packs_dir(self) -> Path:
        """Cache directory for .smolmachine packs built from image archives."""
        return self._provider_dir / "packs"

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

    def _client_ssh_keypair(self) -> tuple[Path, str]:
        """Client keypair mngr uses to reach guests as root."""
        self._keys_dir.mkdir(parents=True, exist_ok=True)
        return load_or_create_ssh_keypair(self._keys_dir, "root_ssh_key")

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
    def _host_store(self) -> SmolvmHostStore:
        """Host record store backed by the state volume."""
        return SmolvmHostStore(volume=self._state_volume)

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

    def _create_host_object(self, host_id: HostId, host_name: HostName, ssh_port: int) -> Host:
        """Create a Host object connecting to the forwarded sshd port."""
        self._record_pre_injected_host_key(host_id, _SSH_HOSTNAME, ssh_port)

        client_key_path, _client_public_key = self._client_ssh_keypair()
        pyinfra_host = create_pyinfra_host(
            hostname=_SSH_HOSTNAME,
            port=ssh_port,
            private_key_path=client_key_path,
            known_hosts_path=self._host_known_hosts_path(host_id),
            ssh_user=_SSH_USER,
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
        """Create an OfflineHost from a host record.

        Wrapped so the offline host is readable (file reads served from its
        persisted volume) whether reached via ``get_host`` or
        ``to_offline_host``; the volume is resolved lazily, so this is free.
        """
        host_id = HostId(host_record.certified_host_data.host_id)
        return make_readable_offline_host(
            OfflineHost(
                id=host_id,
                certified_host_data=host_record.certified_host_data,
                provider_instance=self,
                mngr_ctx=self.mngr_ctx,
                on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                    callback_host_id, certified_data
                ),
            )
        )

    def _provision_ssh(self, machine_name: str, host_id: HostId) -> None:
        """Install and start sshd inside the guest via the smolvm exec channel.

        Idempotent: re-run on every host start to bring sshd back up after a
        reboot (package installs are skipped when already present).
        """
        host_private_key_pem, host_public_key_openssh = self._ensure_host_keypair(host_id)
        _client_key_path, client_public_key = self._client_ssh_keypair()
        script = build_ssh_provisioning_script(
            host_private_key_pem=host_private_key_pem,
            host_public_key_openssh=host_public_key_openssh,
            client_authorized_public_key=client_public_key,
        )
        with log_span("Provisioning sshd in smolvm machine {}", machine_name):
            exit_code, stdout, stderr = smolvm_machine_exec(
                self.mngr_ctx.concurrency_group,
                self.config.smolvm_command,
                machine_name,
                script,
                timeout=self.config.provision_timeout_seconds,
            )
        if exit_code != 0 or "MNGR_PROVISION_OK" not in stdout:
            raise SmolvmProvisioningError(machine_name, stderr.strip() or stdout.strip() or f"exit code {exit_code}")

    def _create_shutdown_script(self, host: Host) -> None:
        """Create the shutdown.sh script inside the VM.

        For smolvm, the script touches the agent's poweroff sentinel; the
        guest agent syncs filesystems and powers the VM off.
        """
        script_content = build_shutdown_script(str(host.host_dir), POWEROFF_SENTINEL_PATH)
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

    def _cleanup_failed_machine(self, machine_name: str) -> None:
        """Best-effort teardown of a half-created smolvm machine.

        Tolerates already-absent machines so it is safe to call from a
        `finally` on any failure path, and swallows concurrency-group
        ``ProcessError``s so a slow cleanup never masks the original failure.
        """
        try:
            smolvm_machine_delete(self.mngr_ctx.concurrency_group, self.config.smolvm_command, machine_name)
        except (SmolvmCommandError, OSError, ProcessError) as cleanup_err:
            logger.debug("Failed to clean up smolvm machine {} during error recovery: {}", machine_name, cleanup_err)

    # =========================================================================
    # Image Archive Packing
    # =========================================================================

    def _pack_from_archive_cached(self, archive_path: Path) -> Path:
        """Convert an image archive into a cached .smolmachine pack.

        The cache key is the archive's content hash, so rebuilding the same
        image is free and a changed image gets a fresh pack. Returns the
        sidecar path for `machine create --from`.
        """
        if not archive_path.exists():
            raise MngrError(f"Image archive not found: {archive_path}")
        content_hash = _sha256_of_file(archive_path)
        self._packs_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = self._packs_dir / f"{content_hash}.smolmachine"
        if sidecar_path.exists():
            logger.debug("Using cached pack for archive {}: {}", archive_path, sidecar_path)
            return sidecar_path
        output_path = self._packs_dir / content_hash
        with log_span("Packing image archive {} (content hash {})", archive_path, content_hash[:12]):
            smolvm_pack_create_from_archive(
                self.mngr_ctx.concurrency_group,
                self.config.smolvm_command,
                archive_path,
                output_path,
            )
        # pack create -o PATH emits PATH (a runnable stub) plus the
        # PATH.smolmachine sidecar; machines are created from the sidecar.
        # The stub is not needed for machine create, so drop it to halve the
        # cache footprint.
        if not sidecar_path.exists():
            raise MngrError(f"pack create did not produce expected sidecar: {sidecar_path}")
        output_path.unlink(missing_ok=True)
        return sidecar_path

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
        """Create a new smolvm machine host."""
        self._ensure_smolvm_available()
        host_id = HostId.generate()
        machine_name = smolvm_machine_name(name, self.mngr_ctx.config.prefix)
        logger.info("Creating smolvm machine host {} ({}) ...", name, machine_name)

        # Resolve the host_dir layout once and lock it in on the host record.
        # The exposed layout (default) shares a host directory into the VM
        # via virtiofs; the btrfs layout attaches a smolvm-managed data disk.
        is_host_data_volume_exposed = self.config.is_host_data_volume_exposed
        data_disk_spec: str | None = None
        volumes: tuple[tuple[str, str], ...] = ()
        if is_host_data_volume_exposed:
            volume_dir = self._ensure_host_volume_dir(host_id)
            volumes = ((str(volume_dir), str(self.host_dir)),)
        else:
            self._ensure_data_disk_support()
            data_disk_spec = f"size={self.config.host_data_disk_size_gb},target={self.host_dir}"

        # Resolve the image source: a pack built from a local archive, an
        # existing pack, an OCI image reference, or bare VM mode (no image).
        parsed_build_args = _parse_build_args(tuple(build_args or ()))
        from_pack: Path | None = parsed_build_args.from_pack
        if parsed_build_args.image_archive is not None:
            from_pack = self._pack_from_archive_cached(parsed_build_args.image_archive)
        image_reference = str(image) if image is not None and from_pack is None else None

        # Pre-generate the keys recorded in known_hosts and injected into the
        # guest during provisioning.
        self._ensure_host_keypair(host_id)
        self._client_ssh_keypair()

        ssh_port = allocate_free_tcp_port()
        effective_start_args = tuple(self.config.default_start_args) + tuple(start_args or ())

        # Tracked so the `finally` can tear down a half-built machine on ANY
        # failure -- including unexpected exceptions that are not
        # MngrError/OSError -- so no orphaned, untracked machine is left behind.
        is_creation_successful = False
        failure_reason = "smolvm host creation was interrupted by an unexpected error"
        try:
            smolvm_machine_create(
                self.mngr_ctx.concurrency_group,
                self.config.smolvm_command,
                machine_name,
                cpus=self.config.default_cpus,
                memory_mib=self.config.default_memory_mib,
                image=image_reference,
                from_pack=from_pack,
                ports=((ssh_port, 22),),
                volumes=volumes,
                data_disk=data_disk_spec,
                extra_args=effective_start_args,
            )
            smolvm_machine_start(
                self.mngr_ctx.concurrency_group,
                self.config.smolvm_command,
                machine_name,
                timeout=self.config.vm_start_timeout_seconds,
            )
            self._provision_ssh(machine_name, host_id)

            with log_span("Waiting for SSH to be ready..."):
                wait_for_sshd(_SSH_HOSTNAME, ssh_port, self.config.ssh_connect_timeout)

            host = self._create_host_object(host_id, name, ssh_port)
            is_creation_successful = True

        except (MngrError, OSError) as e:
            failure_reason = str(e)
            raise SmolvmHostCreationError(self.name, failure_reason) from e
        finally:
            if not is_creation_successful:
                logger.error("smolvm host creation failed; tearing down {}: {}", machine_name, failure_reason)
                self._cleanup_failed_machine(machine_name)
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

        machine_config = SmolvmMachineConfig(
            machine_name=machine_name,
            ssh_host_port=ssh_port,
            image=image_reference,
            from_pack=str(from_pack) if from_pack is not None else None,
            is_host_data_volume_exposed=is_host_data_volume_exposed,
            data_disk_spec=data_disk_spec,
        )
        resources = HostResources(
            cpu=CpuResources(count=self.config.default_cpus),
            memory_gb=self.config.default_memory_mib / 1024.0,
            disk_gb=float(self.config.host_data_disk_size_gb) if data_disk_spec is not None else None,
            gpu=None,
        )
        host_record = HostRecord(
            certified_host_data=host_data,
            ssh_hostname=_SSH_HOSTNAME,
            ssh_port=ssh_port,
            ssh_user=_SSH_USER,
            ssh_identity_file=str(self._client_ssh_keypair()[0]),
            config=machine_config,
            resources=resources,
        )
        self._host_store.write_host_record(host_record)

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
            add_authorized_keys_cmd = build_add_authorized_keys_command(_SSH_USER, tuple(authorized_keys))
            if add_authorized_keys_cmd is not None:
                with log_span("Adding {} authorized_keys entries to VM", len(authorized_keys)):
                    host.execute_stateful_command(f"sh -c '{add_authorized_keys_cmd}'")

        # Add known hosts entries if provided
        if known_hosts:
            add_known_hosts_cmd = build_add_known_hosts_command(_SSH_USER, tuple(known_hosts))
            if add_known_hosts_cmd is not None:
                with log_span("Adding {} known_hosts entries to VM", len(known_hosts)):
                    host.execute_stateful_command(f"sh -c '{add_known_hosts_cmd}'")

        self._evict_cached_host(host_id, replacement=host)
        return host

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop a smolvm machine."""
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Stopping smolvm machine: {}", host_id)

        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.config is not None:
            try:
                smolvm_machine_stop(
                    self.mngr_ctx.concurrency_group,
                    self.config.smolvm_command,
                    host_record.config.machine_name,
                    timeout=max(timeout_seconds, 30.0),
                )
            except SmolvmCommandError as e:
                logger.warning("Error stopping smolvm machine: {}", e)
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
        """Start a stopped smolvm machine."""
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Starting smolvm machine: {}", host_id)

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

        machine_name = host_record.config.machine_name
        ssh_port = host_record.config.ssh_host_port

        try:
            smolvm_machine_start(
                self.mngr_ctx.concurrency_group,
                self.config.smolvm_command,
                machine_name,
                timeout=self.config.vm_start_timeout_seconds,
            )
        except SmolvmCommandError as e:
            raise MngrError(f"Failed to start smolvm machine {host_id}: {e}") from e

        # sshd does not survive a VM stop; re-provision (idempotent, fast
        # when packages are already installed) and wait for connectivity.
        self._provision_ssh(machine_name, host_id)
        with log_span("Waiting for SSH to be ready..."):
            wait_for_sshd(_SSH_HOSTNAME, ssh_port, self.config.ssh_connect_timeout)

        host_obj = self._create_host_object(host_id, HostName(host_record.certified_host_data.host_name), ssh_port)

        # Clear stop reason
        updated_certified = host_record.certified_host_data.model_copy_update(
            to_update(host_record.certified_host_data.field_ref().stop_reason, None),
        )
        self._host_store.write_host_record(
            host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, updated_certified),
            )
        )

        host_obj.record_activity(ActivitySource.BOOT)

        # Restart activity watcher
        with log_span("Restarting activity watcher in VM"):
            start_activity_watcher_cmd = build_start_activity_watcher_command(str(self.host_dir))
            host_obj.execute_stateful_command(f"sh -c '{start_activity_watcher_cmd}'")

        self._evict_cached_host(host_id, replacement=host_obj)
        return host_obj

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Permanently destroy a smolvm machine and mark the host as DESTROYED."""
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Destroying smolvm machine: {}", host_id)

        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.config is not None:
            try:
                # machine rm removes the machine's data directory, including
                # its disks (storage, overlay, and the btrfs data disk).
                smolvm_machine_delete(
                    self.mngr_ctx.concurrency_group,
                    self.config.smolvm_command,
                    host_record.config.machine_name,
                )
            except SmolvmCommandError as e:
                logger.warning("Error deleting smolvm machine: {}", e)

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
        logger.info("Deleting smolvm host records: {}", host_id)

        # Safety net: if the machine somehow outlived destroy_host, remove it
        # before forgetting about it. Tolerates the machine already being absent.
        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.config is not None:
            try:
                smolvm_machine_delete(
                    self.mngr_ctx.concurrency_group,
                    self.config.smolvm_command,
                    host_record.config.machine_name,
                )
            except (SmolvmCommandError, OSError) as e:
                logger.warning("Error deleting smolvm machine during delete_host: {}", e)

        self._host_store.delete_host_record(host_id)

        # Delete volume directory (no-op in btrfs mode: never created).
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

        if host_record.config is None or host_record.ssh_port is None:
            # Failed or offline host
            return self._create_offline_host(host_record)

        # Check if the smolvm machine is running
        machines = smolvm_machine_list(self.mngr_ctx.concurrency_group, self.config.smolvm_command)
        machine_name = host_record.config.machine_name
        is_running = any(
            machine.get("name") == machine_name and machine.get("state") == "running" for machine in machines
        )
        if not is_running:
            return self._create_offline_host(host_record)

        host_obj = self._create_host_object(
            host_id,
            HostName(host_record.certified_host_data.host_name),
            host_record.config.ssh_host_port,
        )
        self._evict_cached_host(host_id, replacement=host_obj)
        return host_obj

    def _get_host_by_name(self, name: HostName) -> HostInterface:
        """Get a host by name."""
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
        """Discover all smolvm hosts managed by this provider instance.

        If smolvm is not installed, returns host records from local state
        only (all marked as offline), so discovery succeeds gracefully in
        environments without smolvm.
        """
        prefix = self.mngr_ctx.config.prefix

        machines: list[dict[str, Any]] = []
        try:
            self._ensure_smolvm_available()
            machines = smolvm_machine_list(cg, self.config.smolvm_command)
        except (SmolvmCommandError, OSError) as e:
            logger.warning("Failed to list smolvm machines: {}", e)
        except ProviderUnavailableError as e:
            logger.debug("smolvm provider not available for discovery: {}", e)

        machine_state_by_name: dict[str, str] = {}
        for machine in machines:
            machine_name = machine.get("name", "")
            if machine_name.startswith(prefix):
                machine_state_by_name[machine_name] = machine.get("state", "unreachable")

        discovered: list[DiscoveredHost] = []
        for record in self._host_store.list_all_host_records():
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)

            if record.config is not None:
                machine_state = machine_state_by_name.pop(record.config.machine_name, None)
                if machine_state is not None:
                    host_state = _SMOLVM_STATE_TO_HOST_STATE.get(machine_state, HostState.CRASHED)
                else:
                    # Machine not found in smolvm -- derive from record
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

        # Surface orphaned smolvm machines: prefix-matched machines that no
        # host record claims. These are leftovers from a create that failed
        # before its record was written; mngr cannot manage them, so warn
        # loudly with the manual cleanup command.
        for orphan_machine_name, orphan_state in machine_state_by_name.items():
            logger.warning(
                "Found orphaned smolvm machine {!r} (state={}) with no mngr host record -- likely a failed or "
                "interrupted create. mngr cannot manage or garbage-collect it; remove it manually with "
                "`smolvm machine rm --force --name {}`.",
                orphan_machine_name,
                orphan_state,
                orphan_machine_name,
            )

        return discovered

    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get configured resources from the persistent host record."""
        host_record = self._host_store.read_host_record(host.id)
        if host_record is not None and host_record.resources is not None:
            return host_record.resources
        return HostResources(
            cpu=CpuResources(count=self.config.default_cpus),
            memory_gb=self.config.default_memory_mib / 1024.0,
            disk_gb=None,
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

        Only hosts created with is_host_data_volume_exposed=True (the
        default) have a host-side volume directory worth listing. btrfs-mode
        hosts deliberately have no host-side directory, so they do not
        appear here even though their data still exists on the in-VM disk.
        """
        volumes: list[VolumeInfo] = []
        if not self._volumes_dir.exists():
            return volumes

        for volume_path in sorted(self._volumes_dir.iterdir()):
            if volume_path.is_dir():
                host_id = HostId(volume_path.name)
                total_size = sum(f.stat().st_size for f in volume_path.rglob("*") if f.is_file())
                volumes.append(
                    VolumeInfo(
                        volume_id=self._volume_id_for_host(host_id),
                        name=f"smolvm-{volume_path.name}",
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
        in that mode host_dir lives only on the in-VM btrfs data disk, so
        the host machine has no direct read path. Callers already handle
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
        raise SmolvmHostRenameError()

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


def _sha256_of_file(path: Path) -> str:
    """Compute the sha256 hex digest of a file in 16 MiB chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
