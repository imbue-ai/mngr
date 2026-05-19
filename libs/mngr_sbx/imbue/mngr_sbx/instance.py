"""Provider instance implementation for Docker Sandboxes (sbx).

The sbx CLI gives us a Docker-like sandbox lifecycle (create / exec / ports /
stop / rm) plus its own OAuth-based authentication to Docker. We bridge it to
mngr's SSH-based host abstraction by installing sshd inside each sandbox, then
publishing the sandbox's port 22 to the host so pyinfra can connect over SSH.

The shape of this module mirrors mngr_lima.instance, with the docker provider's
sshd-bridge pattern replacing Lima's native SSH support.
"""

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
from imbue.mngr.providers.ssh_host_setup import build_configure_ssh_command
from imbue.mngr.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mngr.providers.ssh_host_setup import parse_warnings_from_output
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import clear_host_from_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_sbx.config import SbxProviderConfig
from imbue.mngr_sbx.constants import SBX_AUTH_PROBE_TIMEOUT_SECONDS
from imbue.mngr_sbx.errors import SbxCommandError
from imbue.mngr_sbx.errors import SbxHostCreationError
from imbue.mngr_sbx.errors import SbxHostRenameError
from imbue.mngr_sbx.errors import SbxNotAuthorizedError
from imbue.mngr_sbx.errors import SbxNotInstalledError
from imbue.mngr_sbx.host_store import HostRecord
from imbue.mngr_sbx.host_store import SbxHostConfig
from imbue.mngr_sbx.host_store import SbxHostStore
from imbue.mngr_sbx.host_store import sandbox_name_for_host
from imbue.mngr_sbx.keeper import ensure_sshd_keeper_alive
from imbue.mngr_sbx.keeper import is_keeper_alive
from imbue.mngr_sbx.keeper import kill_keeper_pid
from imbue.mngr_sbx.keeper import read_keeper_pid
from imbue.mngr_sbx.keeper import setup_keeper_command
from imbue.mngr_sbx.keeper import spawn_keeper
from imbue.mngr_sbx.keeper import sshd_keeper_command
from imbue.mngr_sbx.keeper import stop_keeper
from imbue.mngr_sbx.sbx_cli import check_sbx_authenticated
from imbue.mngr_sbx.sbx_cli import sbx_create
from imbue.mngr_sbx.sbx_cli import sbx_exec
from imbue.mngr_sbx.sbx_cli import sbx_list
from imbue.mngr_sbx.sbx_cli import sbx_list_ports
from imbue.mngr_sbx.sbx_cli import sbx_publish_port
from imbue.mngr_sbx.sbx_cli import sbx_rm
from imbue.mngr_sbx.sbx_cli import sbx_stop

# sbx-reported status string -> mngr HostState mapping. Values are lowercased
# before lookup since sbx is not strict about casing across versions.
_SBX_STATUS_TO_HOST_STATE: dict[str, HostState] = {
    "running": HostState.RUNNING,
    "created": HostState.RUNNING,
    "stopped": HostState.STOPPED,
    "exited": HostState.STOPPED,
    "paused": HostState.STOPPED,
    "dead": HostState.CRASHED,
}

# Default SSH user inside the sandbox. sbx's docker-agent image runs as root.
_DEFAULT_SSH_USER: str = "root"

# SSH port inside the sandbox. Constant -- only the host-side mapping varies.
_SANDBOX_SSH_PORT: int = 22


class SbxProviderInstance(BaseProviderInstance):
    """Provider instance for managing Docker Sandboxes (sbx) as mngr hosts.

    Each sandbox runs sshd installed at create time and is accessed via pyinfra
    over a port published by ``sbx ports --publish 22``. Provider state is
    stored in a local volume directory under the profile directory.
    """

    config: SbxProviderConfig = Field(frozen=True, description="sbx provider configuration")

    _sbx_checked: bool = PrivateAttr(default=False)

    # =========================================================================
    # Availability
    # =========================================================================

    def _ensure_sbx_available(self) -> None:
        """Lazily check that sbx is installed and authenticated. Cached for the lifetime of this instance."""
        if self._sbx_checked:
            return
        check_sbx_authenticated(
            self.mngr_ctx.concurrency_group,
            self.name,
            timeout_seconds=SBX_AUTH_PROBE_TIMEOUT_SECONDS,
        )
        self._sbx_checked = True

    @property
    def supports_snapshots(self) -> bool:
        # sbx supports template save/load, but mngr-managed snapshots are not yet implemented.
        return False

    @property
    def supports_shutdown_hosts(self) -> bool:
        # sbx supports stop/start of sandboxes, but the SSH port mapping does not persist
        # across stop+start cycles without re-publishing. Disable until we re-publish in start_host.
        return False

    @property
    def supports_volumes(self) -> bool:
        return False

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
        """Base directory for sbx provider state under the profile dir."""
        return self.mngr_ctx.profile_dir / "providers" / "sbx" / str(self.name)

    @property
    def _keys_dir(self) -> Path:
        return self._provider_dir / "keys"

    @property
    def _known_hosts_path(self) -> Path:
        return self._keys_dir / "known_hosts"

    @cached_property
    def _state_volume(self) -> LocalVolume:
        state_dir = self._provider_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return LocalVolume(root_path=state_dir)

    @cached_property
    def _host_store(self) -> SbxHostStore:
        return SbxHostStore(volume=self._state_volume)

    # =========================================================================
    # SSH key helpers
    # =========================================================================

    def _get_client_keypair(self) -> tuple[Path, str]:
        return load_or_create_ssh_keypair(self._keys_dir, key_name="sbx_ssh_key")

    def _get_host_keypair(self) -> tuple[Path, str]:
        return load_or_create_host_keypair(self._keys_dir)

    # =========================================================================
    # Sandbox exec helpers
    # =========================================================================

    def _exec_in_sandbox(
        self,
        sandbox_name: str,
        command: str,
        detach: bool = False,
        user: str | None = "root",
        timeout: float = 180.0,
    ) -> tuple[int | None, str, str]:
        """Run a shell snippet inside an sbx sandbox via ``sbx exec``."""
        logger.debug(
            "sbx exec sandbox={} user={} cmd_len={} cmd_head={!r}", sandbox_name, user, len(command), command[:200]
        )
        rc, stdout, stderr = sbx_exec(
            self.mngr_ctx.concurrency_group,
            self.name,
            sandbox_name,
            ["sh", "-c", command],
            user=user,
            detach=detach,
            timeout=timeout,
        )
        logger.debug("sbx exec result rc={} stdout_len={} stderr_len={}", rc, len(stdout), len(stderr))
        return rc, stdout, stderr

    def _check_and_install_packages(self, sandbox_name: str) -> None:
        """Ensure sshd, tmux, rsync, etc. are installed inside the sandbox.

        sbx's docker-agent image kicks off an ``apt-get update`` on every sandbox
        start, which holds the dpkg lock for ~20-40 seconds. We deal with this in
        two ways:

        1. Inject ``DPkg::Lock::Timeout=180`` into our apt invocations via
           ``APT_LISTCHANGES_FRONTEND=none`` + an apt.conf snippet, so our calls
           wait for the lock instead of bombing out.
        2. Still try to wait for the boot-time ``apt-get update`` to finish first,
           but only briefly. The lock timeout is the real safety net.

        We bypass mngr's shared ``build_check_and_install_packages_command`` here
        because it doesn't take a lock-timeout flag.
        """
        # The sbx docker-agent image launches a background `apt-get update` on every fresh sandbox
        # start, holding both /var/lib/apt/lists/lock and (briefly) /var/lib/dpkg/lock-frontend.
        # Strategy: tell apt itself to wait for the locks via well-known config options, then run
        # install. apt's built-in lock-wait is more robust than us polling -- pgrep can race, and
        # extra sbx exec calls used to be re-arming the boot apt-update.
        prelude = (
            "set -e; "
            "mkdir -p /etc/apt/apt.conf.d; "
            'printf \'DPkg::Lock::Timeout "300";\\nAcquire::Retries "3";\\n\' '
            ">  /etc/apt/apt.conf.d/99-mngr-sbx-lock-timeout; "
            "export DEBIAN_FRONTEND=noninteractive; "
        )
        host_dir_str = str(self.host_dir)
        # Skip our own `apt-get update`: the boot-time one already populated the cache. Hitting
        # the lists lock ourselves doesn't add anything and just risks contention.
        # Wrap apt-get install in a small bash retry loop -- on rare occasions the boot update
        # leaves the lists lock held briefly after the dpkg lock is freed, and a second attempt
        # always succeeds.
        install_body = (
            "for attempt in 1 2 3; do "
            "  if apt-get install -y -qq openssh-server tmux rsync git jq xxd curl ca-certificates; then "
            "    INSTALL_OK=1; break; "
            "  fi; "
            "  sleep 10; "
            "done; "
            '[ "${INSTALL_OK:-0}" = 1 ] && '
            f"mkdir -p /run/sshd && mkdir -p {host_dir_str}"
        )
        combined_cmd = prelude + install_body
        returncode, stdout, stderr = self._exec_in_sandbox(sandbox_name, combined_cmd, timeout=420.0)
        if returncode != 0:
            raise MngrError(
                f"Failed to install required packages in sandbox {sandbox_name} "
                f"(exit code {returncode}): {stderr or stdout}"
            )
        for warning in parse_warnings_from_output(stdout):
            logger.warning(warning)

    def _provision_ssh_in_sandbox(
        self,
        sandbox_name: str,
        client_public_key: str,
        host_private_key: str,
        host_public_key: str,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
    ) -> None:
        """Install required packages and write SSH key material into the sandbox filesystem.

        sshd itself is *not* started here -- it is started by the keeper (see
        ``sshd_keeper_command``). This split ensures sshd is alive iff the keeper is, so
        revivals after sandbox auto-stop bring sshd back along with the sandbox.
        """
        self._check_and_install_packages(sandbox_name)

        with log_span("Configuring SSH keys in sandbox {}", sandbox_name):
            configure_ssh_cmd = build_configure_ssh_command(
                user=_DEFAULT_SSH_USER,
                client_public_key=client_public_key,
                host_private_key=host_private_key,
                host_public_key=host_public_key,
            )
            returncode, stdout, stderr = self._exec_in_sandbox(sandbox_name, configure_ssh_cmd)
            if returncode != 0:
                raise MngrError(
                    f"Failed to configure SSH in sandbox {sandbox_name} (exit code {returncode}): {stderr or stdout}"
                )

        if known_hosts:
            add_known_hosts_cmd = build_add_known_hosts_command(_DEFAULT_SSH_USER, tuple(known_hosts))
            if add_known_hosts_cmd is not None:
                with log_span("Adding {} known_hosts entries to sandbox", len(known_hosts)):
                    self._exec_in_sandbox(sandbox_name, add_known_hosts_cmd)

        if authorized_keys:
            add_authorized_keys_cmd = build_add_authorized_keys_command(_DEFAULT_SSH_USER, tuple(authorized_keys))
            if add_authorized_keys_cmd is not None:
                with log_span("Adding {} authorized_keys entries to sandbox", len(authorized_keys)):
                    self._exec_in_sandbox(sandbox_name, add_authorized_keys_cmd)

    # =========================================================================
    # Host object helpers
    # =========================================================================

    def _create_host_object(
        self,
        host_id: HostId,
        hostname: str,
        host_port: int,
        private_key_path: Path,
    ) -> Host:
        pyinfra_host = create_pyinfra_host(
            hostname=hostname,
            port=host_port,
            private_key_path=private_key_path,
            known_hosts_path=self._known_hosts_path,
            ssh_user=_DEFAULT_SSH_USER,
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

    def _create_offline_host(self, host_record: HostRecord) -> OfflineHost:
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

    def _on_certified_host_data_updated(self, host_id: HostId, certified_data: CertifiedHostData) -> None:
        with log_span("Updating certified host data", host_id=str(host_id)):
            host_record = self._host_store.read_host_record(host_id, use_cache=False)
            if host_record is None:
                raise HostNotFoundError(host_id)
            updated_host_record = host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, certified_data),
            )
            self._host_store.write_host_record(updated_host_record)

    def _save_failed_host_record(
        self,
        host_id: HostId,
        host_name: HostName,
        tags: Mapping[str, str] | None,
        failure_reason: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(host_name),
            user_tags=dict(tags) if tags else {},
            snapshots=[],
            failure_reason=failure_reason,
            build_log="",
            created_at=now,
            updated_at=now,
        )
        host_record = HostRecord(certified_host_data=host_data)
        self._host_store.write_host_record(host_record)

    def _create_shutdown_script(self, host: Host) -> None:
        """Create the shutdown.sh script inside the sandbox.

        For sbx, shutdown is implemented by signaling PID 1, which terminates the
        sandbox in the same way as the docker provider.
        """
        host_dir_str = str(host.host_dir)
        script_content = f"""#!/bin/bash
# Auto-generated shutdown script for mngr Docker Sandboxes host
# Kills PID 1 to stop the sandbox

LOG_FILE="{host_dir_str}/logs/shutdown.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
    echo "$*"
}}

log "=== Shutdown script started ==="
log "STOP_REASON: ${{1:-PAUSED}}"

kill -TERM 1
"""
        commands_dir = host.host_dir / "commands"
        script_path = commands_dir / "shutdown.sh"
        with log_span("Creating shutdown script at {}", script_path):
            host.write_text_file(script_path, script_content, mode="755")

    # =========================================================================
    # Build args parsing
    # =========================================================================

    def _parse_build_args(
        self,
        build_args: Sequence[str] | None,
        default_workspace: Path,
    ) -> tuple[str, tuple[str, ...], str | None]:
        """Parse mngr-level build args into (workspace, extra_workspaces, template).

        Recognized keys: ``workspace=<path>`` (primary), ``extra-workspace=<spec>``
        (repeatable), ``template=<image>``. Unknown args are passed through into
        ``start_args`` for ``sbx create``.
        """
        workspace = str(default_workspace)
        extras: list[str] = []
        template: str | None = None
        for raw in build_args or ():
            arg = raw.lstrip("-")
            if "=" not in arg:
                continue
            key, value = arg.split("=", 1)
            if key == "workspace":
                workspace = value
            elif key == "extra-workspace":
                extras.append(value)
            elif key == "template":
                template = value
            else:
                # Unrecognized keys are intentionally passed through to sbx create as start_args
                # by the caller; nothing to do here.
                continue
        return workspace, tuple(extras), template or self.config.default_template

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
        if snapshot is not None:
            raise SnapshotsNotSupportedError(self.name)
        self._ensure_sbx_available()

        host_id = HostId.generate()
        sandbox_name = sandbox_name_for_host(str(name), self.mngr_ctx.config.prefix)
        workspace_path, extra_workspaces, template = self._parse_build_args(build_args, default_workspace=Path.cwd())
        effective_start_args = tuple(self.config.default_start_args) + tuple(start_args or ())

        logger.info("Creating Docker Sandbox host {} ({})", name, sandbox_name)

        # Step 1: ask sbx to create the sandbox.
        try:
            sbx_create(
                cg=self.mngr_ctx.concurrency_group,
                provider_name=self.name,
                name=sandbox_name,
                agent_type=self.config.default_agent_type,
                workspace_path=workspace_path,
                extra_workspaces=extra_workspaces,
                template=template,
                cpus=self.config.default_cpus,
                memory=self.config.default_memory,
                extra_args=effective_start_args,
            )
        except (SbxCommandError, SbxNotAuthorizedError, SbxNotInstalledError) as e:
            failure_reason = str(e)
            logger.error("sbx host creation failed during 'sbx create': {}", failure_reason)
            self._save_failed_host_record(host_id, name, tags, failure_reason)
            raise SbxHostCreationError(failure_reason) from e

        # Step 2: spawn the *setup* keeper (sleep) so the sandbox stays alive while we install
        # sshd. We hand over to the long-lived sshd-keeper below; the setup keeper's lifetime
        # ends only AFTER sshd is verified listening so the sandbox never auto-stops mid-handover.
        try:
            setup_handle = spawn_keeper(
                self._provider_dir,
                host_id,
                sandbox_name,
                inner_command=setup_keeper_command(),
            )
        except SbxCommandError as e:
            failure_reason = str(e)
            logger.error("Failed to spawn sbx setup keeper: {}", failure_reason)
            try:
                sbx_rm(self.mngr_ctx.concurrency_group, self.name, sandbox_name, force=True)
            except (SbxCommandError, OSError) as cleanup_err:
                logger.debug("Failed to clean up sandbox {} after keeper failure: {}", sandbox_name, cleanup_err)
            self._save_failed_host_record(host_id, name, tags, failure_reason)
            raise SbxHostCreationError(failure_reason) from e

        # Step 3: install sshd and write SSH key material into the sandbox, then publish a port.
        try:
            private_key_path, client_public_key = self._get_client_keypair()
            host_key_path, host_public_key = self._get_host_keypair()
            host_private_key = host_key_path.read_text()

            self._provision_ssh_in_sandbox(
                sandbox_name,
                client_public_key=client_public_key,
                host_private_key=host_private_key,
                host_public_key=host_public_key,
                known_hosts=known_hosts,
                authorized_keys=authorized_keys,
            )

            # Spawn the long-lived sshd-keeper while the setup keeper is still holding the
            # sandbox open. Once we verify sshd is healthy via wait_for_sshd, we tear down the
            # setup keeper. Both keepers run in parallel briefly -- sbx allows multiple
            # concurrent `sbx exec` sessions on the same sandbox.
            spawn_keeper(
                self._provider_dir,
                host_id,
                sandbox_name,
                inner_command=sshd_keeper_command(),
                as_user="root",
            )

            with log_span("Publishing port 22 on sandbox {}", sandbox_name):
                binding = sbx_publish_port(
                    cg=self.mngr_ctx.concurrency_group,
                    provider_name=self.name,
                    name=sandbox_name,
                    sandbox_port=_SANDBOX_SSH_PORT,
                )

            ssh_hostname = binding.host_ip
            ssh_port = binding.host_port

            # Pre-trust the sandbox's host key so paramiko doesn't prompt.
            self._keys_dir.mkdir(parents=True, exist_ok=True)
            clear_host_from_known_hosts(self._known_hosts_path, ssh_hostname, ssh_port)
            add_host_to_known_hosts(self._known_hosts_path, ssh_hostname, ssh_port, host_public_key)

            with log_span("Waiting for sshd to be ready..."):
                wait_for_sshd(ssh_hostname, ssh_port, self.config.ssh_connect_timeout)

            # sshd is confirmed listening through the published port -- safe to drop the setup
            # keeper now. The sshd-keeper alone holds the sandbox open from here on out.
            kill_keeper_pid(setup_handle.pid)

            host = self._create_host_object(host_id, ssh_hostname, ssh_port, private_key_path)
        except (SbxCommandError, SbxNotAuthorizedError, MngrError, OSError) as e:
            failure_reason = str(e)
            logger.error("sbx sshd bridge setup failed: {}", failure_reason)
            kill_keeper_pid(setup_handle.pid)
            stop_keeper(self._provider_dir, host_id)
            try:
                sbx_rm(self.mngr_ctx.concurrency_group, self.name, sandbox_name, force=True)
            except (SbxCommandError, OSError) as cleanup_err:
                logger.debug("Failed to clean up sandbox {} during error recovery: {}", sandbox_name, cleanup_err)
            self._save_failed_host_record(host_id, name, tags, failure_reason)
            raise SbxHostCreationError(failure_reason) from e

        # Step 3: build the certified host data + record.
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
        sbx_config = SbxHostConfig(
            sandbox_name=sandbox_name,
            agent_type=self.config.default_agent_type,
            workspace_path=workspace_path,
            extra_workspaces=extra_workspaces,
            template=template,
            start_args=effective_start_args,
        )
        host_record = HostRecord(
            certified_host_data=host_data,
            ssh_hostname=ssh_hostname,
            ssh_port=ssh_port,
            ssh_user=_DEFAULT_SSH_USER,
            ssh_identity_file=str(private_key_path),
            ssh_host_public_key=host_public_key,
            config=sbx_config,
            resources=self._infer_resources(),
        )
        self._host_store.write_host_record(host_record)

        host.record_activity(ActivitySource.BOOT)
        host.set_certified_data(host_data)
        self._create_shutdown_script(host)

        with log_span("Starting activity watcher in sandbox {}", sandbox_name):
            start_activity_watcher_cmd = build_start_activity_watcher_command(str(self.host_dir))
            host.execute_stateful_command(f"sh -c '{start_activity_watcher_cmd}'")

        self._evict_cached_host(host_id, replacement=host)
        return host

    def _infer_resources(self) -> HostResources:
        """Best-effort resource record. sbx does not report the resolved values back."""
        cpus = self.config.default_cpus if self.config.default_cpus > 0 else 1
        memory_gb = _parse_memory_gb(self.config.default_memory) if self.config.default_memory else 1.0
        return HostResources(
            cpu=CpuResources(count=int(cpus)),
            memory_gb=memory_gb,
            disk_gb=0.0,
            gpu=None,
        )

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Stopping Docker Sandbox: {}", host_id)
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None or host_record.config is None:
            logger.debug("No host record found for {}; nothing to stop", host_id)
            return

        # Tear down the keeper so the sandbox actually stops (sbx will auto-stop ~3s after
        # the foreground keeper exits, but explicitly calling sbx stop afterward is more deterministic).
        stop_keeper(self._provider_dir, host_id)

        try:
            sbx_stop(
                self.mngr_ctx.concurrency_group,
                self.name,
                host_record.config.sandbox_name,
                timeout=timeout_seconds,
            )
        except SbxCommandError as e:
            logger.warning("Error stopping sbx sandbox {}: {}", host_record.config.sandbox_name, e)

        updated_certified = host_record.certified_host_data.model_copy_update(
            to_update(host_record.certified_host_data.field_ref().stop_reason, HostState.STOPPED.value),
        )
        self._host_store.write_host_record(
            host_record.model_copy_update(
                to_update(host_record.field_ref().certified_host_data, updated_certified),
            )
        )

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        # start_host is intentionally not supported: sbx port publishings are not
        # idempotent across stop/start without explicit re-publishing, and we have
        # not yet implemented the full restart path. Surface this clearly.
        host_id = host.id if isinstance(host, HostInterface) else host
        raise MngrError(
            f"sbx provider does not yet support starting stopped hosts (host {host_id}). "
            "Destroy and recreate the host instead."
        )

    def destroy_host(self, host: HostInterface | HostId) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        logger.info("Destroying Docker Sandbox: {}", host_id)
        if isinstance(host, Host):
            host.disconnect()
        self._evict_cached_host(host_id)
        stop_keeper(self._provider_dir, host_id)

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is not None and host_record.config is not None:
            try:
                sbx_rm(
                    self.mngr_ctx.concurrency_group,
                    self.name,
                    host_record.config.sandbox_name,
                    force=True,
                )
            except SbxCommandError as e:
                logger.warning("Error removing sbx sandbox {}: {}", host_record.config.sandbox_name, e)

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
        host_id = host.id
        logger.info("Deleting sbx host records: {}", host_id)
        self._host_store.delete_host_record(host_id)
        self._evict_cached_host(host_id)

    def on_connection_error(self, host_id: HostId) -> None:
        self._evict_cached_host(host_id)

    # =========================================================================
    # Discovery Methods
    # =========================================================================

    def get_host(self, host: HostId | HostName) -> HostInterface:
        if isinstance(host, HostId):
            return self._get_host_by_id(host)
        return self._get_host_by_name(host)

    def _get_host_by_id(self, host_id: HostId) -> HostInterface:
        if host_id in self._host_by_id_cache:
            return self._host_by_id_cache[host_id]

        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(host_id)

        if (
            host_record.config is None
            or host_record.ssh_hostname is None
            or host_record.ssh_port is None
            or host_record.ssh_identity_file is None
        ):
            return self._create_offline_host(host_record)

        # Once a host has been marked stopped or destroyed in its record, never bring the keeper
        # back -- that would resurrect a sandbox the user (or GC) just asked us to tear down.
        is_terminal_state = host_record.certified_host_data.stop_reason in (
            HostState.STOPPED.value,
            HostState.DESTROYED.value,
        )
        if is_terminal_state:
            return self._create_offline_host(host_record)

        sandbox_name = host_record.config.sandbox_name
        ssh_identity_file = host_record.ssh_identity_file

        # If the keeper died, the sandbox auto-stops within a few seconds. The sshd-keeper revives
        # both the sandbox AND sshd inside it, but sbx loses port mappings on stop -- so we also
        # need to re-publish port 22 below and update the host record's ssh_port.
        keeper_pid_before = read_keeper_pid(self._provider_dir, host_id)
        try:
            keeper_handle = ensure_sshd_keeper_alive(self._provider_dir, host_id, sandbox_name)
        except SbxCommandError as e:
            logger.debug("Could not revive sshd keeper for sandbox {}: {}", sandbox_name, e)
            return self._create_offline_host(host_record)
        # A different pid after the call means ensure_sshd_keeper_alive had to spawn a fresh
        # keeper -- which implies the sandbox auto-stopped, dropping its port mapping. Comparing
        # before/after pids is strictly more reliable than checking is_keeper_alive separately,
        # because it observes the actual outcome rather than racing the death window.
        keeper_was_respawned = keeper_pid_before is None or keeper_handle.pid != keeper_pid_before

        try:
            sandboxes = sbx_list(self.mngr_ctx.concurrency_group, self.name)
        except (SbxCommandError, SbxNotAuthorizedError, SbxNotInstalledError) as e:
            logger.debug("sbx ls failed during get_host: {}", e)
            return self._create_offline_host(host_record)

        matching = next((s for s in sandboxes if s.name == sandbox_name), None)
        if matching is None or _host_state_from_sbx_status(matching.status) != HostState.RUNNING:
            return self._create_offline_host(host_record)

        # If we just revived the keeper, the sandbox lost its port mapping during auto-stop --
        # re-publish 22 and update the record + known_hosts so subsequent SSH attempts hit the
        # right port.
        effective_ssh_hostname = host_record.ssh_hostname
        effective_ssh_port = host_record.ssh_port
        if keeper_was_respawned:
            try:
                host_record = self._refresh_published_ssh_port(host_record)
            except (SbxCommandError, MngrError) as e:
                logger.debug("Could not re-publish ssh port for {}: {}", sandbox_name, e)
                return self._create_offline_host(host_record)
            if host_record.ssh_hostname is None or host_record.ssh_port is None:
                return self._create_offline_host(host_record)
            effective_ssh_hostname = host_record.ssh_hostname
            effective_ssh_port = host_record.ssh_port
            try:
                wait_for_sshd(effective_ssh_hostname, effective_ssh_port, self.config.ssh_connect_timeout)
            except MngrError as e:
                logger.warning("sshd not ready after keeper revival for {}: {}", sandbox_name, e)
                return self._create_offline_host(host_record)

        host_obj = self._create_host_object(
            host_id,
            hostname=effective_ssh_hostname,
            host_port=effective_ssh_port,
            private_key_path=Path(ssh_identity_file),
        )
        self._evict_cached_host(host_id, replacement=host_obj)
        return host_obj

    def _refresh_published_ssh_port(self, host_record: HostRecord) -> HostRecord:
        """Re-discover or re-publish the host-side mapping for the sandbox's port 22.

        Called after a keeper revival, when sbx has freshly restarted the sandbox and dropped any
        prior port bindings. Returns the updated HostRecord with the (possibly new) ssh_port
        applied, and persists it. Also rewrites known_hosts so the saved host key is associated
        with the new port.
        """
        if host_record.config is None:
            raise MngrError("Cannot refresh ssh port without a host config")
        sandbox_name = host_record.config.sandbox_name

        bindings = sbx_list_ports(self.mngr_ctx.concurrency_group, self.name, sandbox_name)
        existing = next(
            (b for b in bindings if b.sandbox_port == _SANDBOX_SSH_PORT and b.protocol.startswith("tcp")),
            None,
        )
        if existing is not None:
            binding = existing
        else:
            with log_span("Re-publishing port 22 on sandbox {}", sandbox_name):
                binding = sbx_publish_port(
                    cg=self.mngr_ctx.concurrency_group,
                    provider_name=self.name,
                    name=sandbox_name,
                    sandbox_port=_SANDBOX_SSH_PORT,
                )

        if host_record.ssh_hostname == binding.host_ip and host_record.ssh_port == binding.host_port:
            return host_record

        logger.debug(
            "sbx port mapping for {} changed: {}:{} -> {}:{}",
            sandbox_name,
            host_record.ssh_hostname,
            host_record.ssh_port,
            binding.host_ip,
            binding.host_port,
        )

        # Refresh known_hosts: remove any stale entries for the old port, add the new one.
        if host_record.ssh_host_public_key is not None:
            self._keys_dir.mkdir(parents=True, exist_ok=True)
            if host_record.ssh_hostname is not None and host_record.ssh_port is not None:
                clear_host_from_known_hosts(self._known_hosts_path, host_record.ssh_hostname, host_record.ssh_port)
            clear_host_from_known_hosts(self._known_hosts_path, binding.host_ip, binding.host_port)
            add_host_to_known_hosts(
                self._known_hosts_path,
                binding.host_ip,
                binding.host_port,
                host_record.ssh_host_public_key,
            )

        updated_record = host_record.model_copy_update(
            to_update(host_record.field_ref().ssh_hostname, binding.host_ip),
            to_update(host_record.field_ref().ssh_port, binding.host_port),
        )
        self._host_store.write_host_record(updated_record)
        return updated_record

    def _get_host_by_name(self, name: HostName) -> HostInterface:
        for record in self._host_store.list_all_host_records():
            if record.certified_host_data.host_name == str(name):
                return self._get_host_by_id(HostId(record.certified_host_data.host_id))
        raise HostNotFoundError(name)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        host_record = self._host_store.read_host_record(host_id, use_cache=False)
        if host_record is None:
            raise HostNotFoundError(host_id)
        return self._create_offline_host(host_record)

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        """Discover all sbx-managed hosts.

        If sbx is unavailable or unauthenticated, falls back to host records only
        (all marked offline) so the rest of mngr can still see destroyed hosts
        for cleanup purposes.
        """
        prefix = self.mngr_ctx.config.prefix
        local_records = self._host_store.list_all_host_records()

        # If we have no local records, we can short-circuit without invoking the sbx CLI at all.
        # This keeps test runs that never created an sbx host from tripping the sbx resource
        # guard (and avoids burning network/auth on a provider the caller isn't actually using).
        if not local_records:
            return []

        live_status_by_name: dict[str, str] = {}
        try:
            self._ensure_sbx_available()
            for sandbox in sbx_list(cg, self.name):
                if sandbox.name.startswith(prefix):
                    live_status_by_name[sandbox.name] = sandbox.status
        except (SbxNotInstalledError, ProviderUnavailableError) as e:
            logger.debug("sbx provider not available for discovery: {}", e)
        except SbxNotAuthorizedError as e:
            logger.warning("sbx provider not authenticated: {}", e)
        except (SbxCommandError, OSError) as e:
            logger.warning("Failed to list sbx sandboxes: {}", e)

        discovered: list[DiscoveredHost] = []
        for record in local_records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)
            keeper_pid = read_keeper_pid(self._provider_dir, host_id)
            host_state = _derive_host_state(record, live_status_by_name, keeper_pid)
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
        host_record = self._host_store.read_host_record(host.id)
        if host_record is not None and host_record.resources is not None:
            return host_record.resources
        return HostResources(
            cpu=CpuResources(count=1),
            memory_gb=1.0,
            disk_gb=0.0,
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

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        return []

    def delete_snapshot(self, host: HostInterface | HostId, snapshot_id: SnapshotId) -> None:
        raise SnapshotsNotSupportedError(self.name)

    # =========================================================================
    # Volume Methods (not supported)
    # =========================================================================

    def list_volumes(self) -> list[VolumeInfo]:
        return []

    def delete_volume(self, volume_id: VolumeId) -> None:
        raise MngrError(f"sbx provider does not support volume deletion (volume_id={volume_id})")

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        return None

    # =========================================================================
    # Tag Methods
    # =========================================================================

    def _read_tags(self, host_id: HostId) -> dict[str, str]:
        record = self._host_store.read_host_record(host_id)
        if record is None:
            raise HostNotFoundError(host_id)
        return dict(record.certified_host_data.user_tags)

    def _write_tags(self, host_id: HostId, tags: Mapping[str, str]) -> None:
        record = self._host_store.read_host_record(host_id, use_cache=False)
        if record is None:
            raise HostNotFoundError(host_id)
        updated_certified = record.certified_host_data.model_copy_update(
            to_update(record.certified_host_data.field_ref().user_tags, dict(tags)),
            to_update(record.certified_host_data.field_ref().updated_at, datetime.now(timezone.utc)),
        )
        self._host_store.write_host_record(
            record.model_copy_update(
                to_update(record.field_ref().certified_host_data, updated_certified),
            )
        )

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
        raise SbxHostRenameError()

    # =========================================================================
    # Connector Method
    # =========================================================================

    def get_connector(self, host: HostInterface | HostId) -> PyinfraHost:
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


def _host_state_from_sbx_status(status: str) -> HostState:
    """Map an sbx status string to a mngr HostState. Unknown values map to CRASHED."""
    return _SBX_STATUS_TO_HOST_STATE.get(status.lower(), HostState.CRASHED)


def _derive_host_state(
    record: HostRecord,
    live_status_by_name: Mapping[str, str],
    keeper_pid: int | None,
) -> HostState:
    """Combine the local host record with the live sbx-reported status to derive HostState.

    A keeper PID with no live process implies the sandbox is racing toward auto-stop; we
    report STOPPED so mngr does not try to use a stale SSH endpoint.
    """
    if record.config is None:
        return HostState.FAILED

    live_status = live_status_by_name.get(record.config.sandbox_name)
    if live_status is not None:
        derived = _host_state_from_sbx_status(live_status)
        # Even if sbx still reports 'running', a dead keeper means the sandbox is about to
        # auto-stop -- demote to STOPPED so callers don't queue work against a doomed host.
        if derived == HostState.RUNNING and keeper_pid is not None and not is_keeper_alive(keeper_pid):
            return HostState.STOPPED
        return derived

    if record.certified_host_data.failure_reason is not None:
        return HostState.FAILED
    stop_reason = record.certified_host_data.stop_reason
    if stop_reason == HostState.DESTROYED.value:
        return HostState.DESTROYED
    if stop_reason == HostState.STOPPED.value:
        return HostState.STOPPED
    return HostState.CRASHED


def _parse_memory_gb(value: str) -> float:
    """Parse an sbx memory string ('4g', '2048m', '1024k') into GB."""
    text = value.strip().lower()
    if text.endswith("g"):
        try:
            return float(text[:-1])
        except ValueError:
            return 1.0
    if text.endswith("m"):
        try:
            return float(text[:-1]) / 1024.0
        except ValueError:
            return 1.0
    if text.endswith("k"):
        try:
            return float(text[:-1]) / (1024.0 * 1024.0)
        except ValueError:
            return 1.0
    try:
        return float(text)
    except ValueError:
        return 1.0
