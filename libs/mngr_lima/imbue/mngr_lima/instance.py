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
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
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
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.constants import CLOUD_INIT_TIMEOUT_SECONDS
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
from imbue.mngr_lima.limactl import limactl_list
from imbue.mngr_lima.limactl import limactl_shell
from imbue.mngr_lima.limactl import limactl_show_ssh
from imbue.mngr_lima.limactl import limactl_start_existing
from imbue.mngr_lima.limactl import limactl_start_new
from imbue.mngr_lima.limactl import limactl_stop

# Lima instance status values mapped to mngr HostState
_LIMA_STATUS_TO_HOST_STATE: dict[str, HostState] = {
    "Running": HostState.RUNNING,
    "Stopped": HostState.STOPPED,
    "Broken": HostState.CRASHED,
    "Unknown": HostState.CRASHED,
}


class LimaProviderInstance(BaseProviderInstance):
    """Provider instance for managing Lima VMs as hosts.

    Each VM runs Lima's default user (matching the host username) with
    passwordless sudo. SSH access is managed entirely by Lima. The provider
    uses a local volume directory for persistent host state.
    """

    config: LimaProviderConfig = Field(frozen=True, description="Lima provider configuration")

    _host_by_id_cache: dict[HostId, HostInterface] = PrivateAttr(default_factory=dict)
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
        self._host_by_id_cache.clear()
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

    @property
    def _known_hosts_path(self) -> Path:
        """Path to the known_hosts file for this provider instance."""
        return self._keys_dir / "known_hosts"

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
        ssh_config: LimaSshConfig,
    ) -> Host:
        """Create a Host object from SSH connection info."""
        # Get the host key via ssh-keyscan and add to known_hosts
        self._keys_dir.mkdir(parents=True, exist_ok=True)

        # Add the host to known_hosts by scanning its key
        self._scan_and_add_host_key(ssh_config.hostname, ssh_config.port)

        pyinfra_host = create_pyinfra_host(
            hostname=ssh_config.hostname,
            port=ssh_config.port,
            private_key_path=ssh_config.identity_file,
            known_hosts_path=self._known_hosts_path,
        )
        connector = PyinfraConnector(pyinfra_host)

        return Host(
            id=host_id,
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            on_updated_host_data=lambda callback_host_id, certified_data: self._on_certified_host_data_updated(
                callback_host_id, certified_data
            ),
        )

    def _scan_and_add_host_key(self, hostname: str, port: int) -> None:
        """Scan the SSH host key and add it to known_hosts."""
        result = self.mngr_ctx.concurrency_group.run_process_to_completion(
            ["ssh-keyscan", "-p", str(port), hostname],
            timeout=10.0,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Parse the first key line from ssh-keyscan output
            for line in result.stdout.strip().splitlines():
                if line and not line.startswith("#"):
                    # Extract just the key type and key data
                    parts = line.split(None, 2)
                    if len(parts) >= 3:
                        key_type_and_data = f"{parts[1]} {parts[2]}"
                        add_host_to_known_hosts(self._known_hosts_path, hostname, port, key_type_and_data)
                        return
        logger.warning("Could not scan SSH host key for {}:{}", hostname, port)

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData) -> None:
        """Update the certified host data in the host record."""
        with log_span("Updating certified host data", host_id=str(host_id)):
            host_record = self._host_store.read_host_record(host_id, use_cache=False)
            if host_record is None:
                raise HostNotFoundError(host_id)
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
        host_id = HostId.generate()
        instance_name = lima_instance_name(name, self.mngr_ctx.config.prefix)
        logger.info("Creating Lima VM host {} ({}) ...", name, instance_name)

        # Create the persistent volume directory
        volume_dir = self._ensure_host_volume_dir(host_id)

        # Generate or load Lima YAML config
        yaml_path_from_build_args = parse_build_args_for_yaml_path(tuple(build_args or ()))
        if yaml_path_from_build_args is not None:
            user_config = load_user_lima_yaml(yaml_path_from_build_args)
            base_config = generate_default_lima_yaml(
                volume_host_path=volume_dir,
                host_dir=str(self.host_dir),
                config_image_url_aarch64=self.config.default_image_url_aarch64,
                config_image_url_x86_64=self.config.default_image_url_x86_64,
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
            )

        # Write the YAML config to a temp file
        yaml_path = write_lima_yaml(lima_config)

        effective_start_args = tuple(self.config.default_start_args) + tuple(start_args or ())

        try:
            # Create and start the Lima instance
            limactl_start_new(
                self.mngr_ctx.concurrency_group,
                instance_name,
                yaml_path,
                start_args=effective_start_args,
            )

            # Wait for cloud-init to complete
            self._wait_for_cloud_init(instance_name)

            # Get SSH connection info
            ssh_config = self._get_ssh_config(instance_name)

            # Wait for SSH to be ready
            with log_span("Waiting for SSH to be ready..."):
                wait_for_sshd(ssh_config.hostname, ssh_config.port, self.config.ssh_connect_timeout)

            # Create the Host object
            host = self._create_host_object(host_id, ssh_config)

        except (LimaCommandError, MngrError, OSError) as e:
            failure_reason = str(e)
            logger.error("Lima host creation failed: {}", failure_reason)
            # Clean up the Lima instance
            try:
                limactl_delete(self.mngr_ctx.concurrency_group, instance_name, force=True)
            except (LimaCommandError, OSError) as cleanup_err:
                logger.debug(
                    "Failed to clean up Lima instance {} during error recovery: {}", instance_name, cleanup_err
                )
            self._save_failed_host_record(
                host_id=host_id,
                host_name=name,
                tags=tags,
                failure_reason=failure_reason,
                build_log="",
            )
            raise LimaHostCreationError(failure_reason) from e
        finally:
            # Clean up the temporary YAML config file
            yaml_path.unlink(missing_ok=True)

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

        self._host_by_id_cache[host_id] = host
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
        cached_host = self._host_by_id_cache.get(host_id)
        if isinstance(cached_host, Host):
            cached_host.disconnect()

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

        # Update host record with stop reason
        if host_record is not None:
            updated_certified_data = host_record.certified_host_data.model_copy_update(
                to_update(host_record.certified_host_data.field_ref().stop_reason, HostState.STOPPED.value),
            )
            self._host_store.write_host_record(
                host_record.model_copy_update(
                    to_update(host_record.field_ref().certified_host_data, updated_certified_data),
                )
            )

        self._host_by_id_cache.pop(host_id, None)

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
            raise HostNotFoundError(host_id)

        if host_record.config is None:
            raise MngrError(f"Host {host_id} has no configuration and cannot be started.")

        if host_record.certified_host_data.failure_reason is not None:
            raise MngrError(
                f"Host {host_id} failed during creation and cannot be started. "
                f"Reason: {host_record.certified_host_data.failure_reason}"
            )

        instance_name = host_record.config.instance_name

        try:
            limactl_start_existing(self.mngr_ctx.concurrency_group, instance_name)
        except LimaCommandError as e:
            raise MngrError(f"Failed to start Lima VM {host_id}: {e}") from e

        # Get SSH info and wait for connectivity
        ssh_config = self._get_ssh_config(instance_name)
        with log_span("Waiting for SSH to be ready..."):
            wait_for_sshd(ssh_config.hostname, ssh_config.port, self.config.ssh_connect_timeout)

        host_obj = self._create_host_object(host_id, ssh_config)

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

        self._host_by_id_cache[host_id] = host_obj
        return host_obj

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Permanently destroy a Lima VM and delete its snapshots."""
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Destroying Lima VM: {}", host_id)

        # Disconnect SSH
        cached_host = self._host_by_id_cache.get(host_id)
        if isinstance(cached_host, Host):
            cached_host.disconnect()

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.config is not None:
            try:
                limactl_delete(self.mngr_ctx.concurrency_group, host_record.config.instance_name, force=True)
            except LimaCommandError as e:
                logger.warning("Error deleting Lima instance: {}", e)

        # Mark as destroyed in host record
        if host_record is not None:
            updated_certified = host_record.certified_host_data.model_copy_update(
                to_update(host_record.certified_host_data.field_ref().stop_reason, HostState.DESTROYED.value),
            )
            self._host_store.write_host_record(
                host_record.model_copy_update(
                    to_update(host_record.field_ref().certified_host_data, updated_certified),
                )
            )

        self._host_by_id_cache.pop(host_id, None)

    def delete_host(self, host: HostInterface) -> None:
        """Permanently delete all records associated with a destroyed host."""
        host_id = host.id
        logger.info("Deleting Lima host records: {}", host_id)

        # Delete host record from store
        self._host_store.delete_host_record(host_id)

        # Delete volume directory
        volume_dir = self._get_host_volume_dir(host_id)
        if volume_dir.exists():
            shutil.rmtree(volume_dir, ignore_errors=True)

        # Delete tags file
        tags_path = self._tags_path(host_id)
        if tags_path.exists():
            tags_path.unlink(missing_ok=True)

        self._host_by_id_cache.pop(host_id, None)

    def on_connection_error(self, host_id: HostId) -> None:
        """Handle connection errors by clearing the cache."""
        self._host_by_id_cache.pop(host_id, None)

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
            raise HostNotFoundError(host_id)

        if host_record.config is None or host_record.ssh_hostname is None:
            # Failed or offline host
            return self._create_offline_host(host_record)

        # Check if the Lima instance is running
        instances = limactl_list(self.mngr_ctx.concurrency_group)
        instance_name = host_record.config.instance_name
        is_running = any(inst.get("name") == instance_name and inst.get("status") == "Running" for inst in instances)

        if not is_running:
            return self._create_offline_host(host_record)

        # Instance is running -- create online host
        ssh_config = self._get_ssh_config(instance_name)
        host_obj = self._create_host_object(host_id, ssh_config)
        self._host_by_id_cache[host_id] = host_obj
        return host_obj

    def _get_host_by_name(self, name: HostName) -> HostInterface:
        """Get a host by name."""
        # Search through host records
        for record in self._host_store.list_all_host_records():
            if record.certified_host_data.host_name == str(name):
                host_id = HostId(record.certified_host_data.host_id)
                return self._get_host_by_id(host_id)
        raise HostNotFoundError(name)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Return an offline representation of the given host."""
        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(host_id)
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
        """List all volumes managed by this provider."""
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
        """Get the host volume for a given host."""
        host_id = host.id if isinstance(host, HostInterface) else host
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
