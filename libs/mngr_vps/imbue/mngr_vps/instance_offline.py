import shlex
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import validate_and_create_discovered_agent
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr_vps.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps.host_state_store import HostDirBackend
from imbue.mngr_vps.host_state_store import HostStateStore
from imbue.mngr_vps.host_state_store import NullHostDirBackend
from imbue.mngr_vps.host_state_store import StateBucket
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.systemd import render_systemd_unit

IDLE_SENTINEL_FILENAME: Final[str] = "stop-instance-requested"

# Self-stopping idle watcher (host-side), shared by every offline-capable provider.
# The in-container activity watcher writes ``IDLE_SENTINEL_FILENAME`` onto the
# shared volume when idle; a host-side systemd ``.path`` unit (this unit name)
# watches the corresponding outer-filesystem path and triggers a oneshot
# ``.service`` that stops the instance. The action the ``.service`` takes is
# per-provider (AWS/GCP power the host off via ``shutdown -P now``; Azure runs an
# ARM self-deallocate, since an OS shutdown does not halt Azure compute billing) --
# see ``OfflineCapableVpsProvider._idle_watcher_service_unit``.
IDLE_WATCHER_UNIT_NAME: Final[str] = "mngr-idle-watcher"

# Host-side host_dir-to-bucket sync daemon (Component 3 of specs/provider-state-bucket).
# When a provider syncs host_dir to an object store, the create path installs (over
# SSH on the outer) a systemd oneshot ``.service`` + ``.timer`` pair: every
# ``HOST_DIR_SYNC_INTERVAL_SECONDS`` the oneshot syncs the per-host ``host_dir`` tree
# to the bucket (AWS: ``aws s3 sync``; Azure: ``azcopy sync``). The same oneshot is
# triggered once on graceful stop so the offline copy is current. GCP does not sync
# host_dir (no object store), so its gate is off and nothing is installed.
HOST_DIR_SYNC_UNIT_NAME: Final[str] = "mngr-host-dir-sync"
HOST_DIR_SYNC_INTERVAL_SECONDS: Final[int] = 60

# Host-side scripts the oneshot ``.service`` units run via ``ExecStart``. Installing
# the command as a script (rather than an inline ``ExecStart=/bin/sh -c '...'``) keeps
# the embedded paths/URIs out of systemd's + the shell's nested quoting.
HOST_DIR_SYNC_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-host-dir-sync.sh"
# Default (AWS/GCP) idle action: the idle-watcher ``.service`` runs this poweroff
# script. Azure overrides the action with its own ARM self-deallocate script.
IDLE_WATCHER_POWEROFF_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-idle-watcher.sh"


def build_sentinel_shutdown_script(sentinel_in_container: str) -> str:
    """Build the in-container ``shutdown.sh`` that signals idle by touching the sentinel.

    Unlike the base ``VpsProvider`` shutdown script (which stops only the
    container), the self-stopping cloud variant only *signals* idle: it touches a
    sentinel file on the shared volume. The host-side systemd path unit observes
    that file and stops the whole instance (a container cannot stop its host, so
    the signal has to cross the container boundary via the shared volume).
    """
    return f'#!/bin/bash\ntouch "{sentinel_in_container}"\n'


def build_idle_watcher_path_unit(sentinel_on_outer: str, instance_kind: str) -> str:
    """Build the systemd ``.path`` unit that fires when the idle sentinel appears.

    ``PathExists`` triggers the paired ``.service`` once the sentinel file exists at
    ``sentinel_on_outer`` (the outer-filesystem location the container's sentinel
    write maps to on the per-host btrfs subvolume). ``instance_kind`` is the
    provider's wording for the machine (``EC2 instance`` / ``GCE instance`` /
    ``Azure VM``), used only in the human-readable ``Description=``.
    """
    return render_systemd_unit(
        {
            "Unit": [("Description", f"Watch for the mngr idle sentinel and stop this {instance_kind} when idle")],
            "Path": [("PathExists", sentinel_on_outer), ("Unit", f"{IDLE_WATCHER_UNIT_NAME}.service")],
            "Install": [("WantedBy", "multi-user.target")],
        }
    )


def build_poweroff_idle_watcher_script(sentinel_on_outer: str) -> str:
    """Build the host-side script that powers the instance off when mngr signals idle.

    Installed at ``IDLE_WATCHER_POWEROFF_SCRIPT_PATH`` and run by the idle-watcher
    ``.service``. Powers off with ``shutdown -P now``; on AWS EC2 that then applies
    the instance's ``InstanceInitiatedShutdownBehavior`` (stop or terminate), and on
    GCE a guest poweroff lands the instance in ``TERMINATED`` (stopped, disk
    preserved, no compute billing) -- both with no IAM/API call.

    It removes the sentinel file BEFORE powering off. This is what makes resume
    work: when ``mngr start`` boots the instance again, systemd re-arms the ``.path``
    unit -- if the sentinel were still present it would fire immediately and re-stop
    the just-resumed instance. Clearing it first guarantees a clean slate on the next
    boot (the in-container watcher only re-creates it if the host is idle again).
    """
    return (
        "#!/bin/sh\n"
        "# Installed by mngr -- power this instance off when it signals idle.\n"
        "set -u\n"
        f"rm -f {shlex.quote(sentinel_on_outer)}\n"
        "shutdown -P now\n"
    )


def build_poweroff_idle_watcher_service_unit() -> str:
    """Build the oneshot systemd ``.service`` that runs the installed poweroff script.

    ``ExecStart`` points at ``IDLE_WATCHER_POWEROFF_SCRIPT_PATH`` (see
    ``build_poweroff_idle_watcher_script``) rather than an inline ``/bin/sh -c``, so
    the sentinel path it removes never has to survive systemd + shell quoting.
    """
    return render_systemd_unit(
        {
            "Unit": [("Description", "Power off this instance when mngr signals the host is idle")],
            "Service": [("Type", "oneshot"), ("ExecStart", IDLE_WATCHER_POWEROFF_SCRIPT_PATH)],
        }
    )


def build_host_dir_sync_timer_unit(interval_seconds: int) -> str:
    """Build the systemd ``.timer`` that fires the host_dir sync every ``interval_seconds``.

    ``OnBootSec`` gives the host a moment to finish bootstrapping before the first
    sync; ``OnUnitActiveSec`` then repeats at the interval. Shared by AWS and Azure.
    """
    return render_systemd_unit(
        {
            "Unit": [("Description", "Periodically sync this host's host_dir to the mngr state bucket")],
            "Timer": [
                ("OnBootSec", str(interval_seconds)),
                ("OnUnitActiveSec", str(interval_seconds)),
                ("Unit", f"{HOST_DIR_SYNC_UNIT_NAME}.service"),
            ],
            "Install": [("WantedBy", "timers.target")],
        }
    )


def build_host_dir_sync_script(sync_command: str) -> str:
    """Build the host-side script the host_dir-sync ``.service`` runs via ``ExecStart``.

    Installed at ``HOST_DIR_SYNC_SCRIPT_PATH``. Wrapping the provider's sync command
    (``aws s3 sync ...`` / ``azcopy sync ...``) in a script -- rather than an inline
    ``ExecStart=/bin/sh -c '...'`` -- keeps the embedded host_dir path and bucket URI
    out of systemd's + the shell's nested quoting.
    """
    return f"#!/bin/sh\nexec {sync_command}\n"


class OfflineCapableVpsProvider(VpsProvider):
    """``VpsProvider`` for cloud providers whose hosts can be stopped while
    their disk persists, with host/agent identity mirrored into instance
    tags/metadata.

    A stopped (deallocated / powered-off) instance keeps its disk but is
    SSH-unreachable, so the volume-backed base discovery and host resolution
    cannot see it. This class adds the shared "offline" recovery: it reconstructs
    such hosts (and their agents) from the provider's instance listing, and falls
    back to that listing whenever the on-volume path raises ``HostNotFoundError``.

    Subclasses (AWS/GCP/Azure) supply the per-provider instance-data hooks below.
    The agent-record *write* side (``persist_agent_data`` /
    ``remove_persisted_agent_data``) stays provider-specific because the tag vs
    metadata write APIs differ too much to share.

    It also owns the shared cloud stop/start lifecycle: ``stop_host`` pauses the
    whole instance (so a paused agent costs only disk) and ``start_host`` resumes
    it, with the record-write + external mirror in one place. Providers supply only
    the cloud-API hooks (``_pause_cloud_instance`` / ``_resume_cloud_instance``) and
    override ``_sync_host_dir_before_pause`` / the known_hosts rebind where their
    behavior differs.
    """

    # =========================================================================
    # Cloud stop/start lifecycle (idle-pause + resume)
    #
    # The base ``VpsProvider`` stop/start act only on the inner placement (the
    # container, for the Docker realizer). A cloud instance that keeps its disk
    # while stopped is paused as a whole on stop -- so a paused agent costs only
    # disk -- and resumed on start. This orchestration lives here once; providers
    # supply the small cloud-API hooks. Keeping the record-write + external mirror
    # in a single place means a resumed host's offline view is always refreshed, on
    # every provider (a per-provider copy once dropped the Azure mirror).
    # =========================================================================

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
        stop_reason: HostState | None = None,
    ) -> None:
        """Stop the agent placement *and* pause the cloud instance, preserving its disk.

        The base ``VpsProvider.stop_host`` stops only the inner placement, leaving
        the instance running and billing. This reuses that placement-stop +
        record-write via ``super()`` (passing ``stop_reason=STOPPED`` so the single
        write marks the host STOPPED before its volume goes unreachable -- the
        offline-state derivation then reports STOPPED, not CRASHED), then pauses the
        instance via ``_pause_cloud_instance`` so a paused agent costs only disk.
        The disk (and all on-disk state) survives, so ``start_host`` can resume it.
        ``create_snapshot`` is ignored -- pausing preserves the whole filesystem.
        """
        del create_snapshot
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        super().stop_host(
            host, create_snapshot=False, timeout_seconds=timeout_seconds, stop_reason=stop_reason or HostState.STOPPED
        )
        # The placement is stopped (host_dir quiesced) but the instance is still
        # reachable: flush any offline host_dir mirror now, before the pause.
        self._sync_host_dir_before_pause(host_id, host_record.vps_ip)
        self._pause_cloud_instance(host_record.config.vps_instance_id)

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Resume a paused agent: start the cloud instance, then its placement.

        A paused instance is SSH-unreachable, so it is located by its
        ``mngr-host-id`` tag/label (not the SSH-based record lookup), resumed via
        ``_resume_cloud_instance`` (which returns the instance's SSH address --
        fresh for ephemeral-IP providers, unchanged for a static IP), and its
        known_hosts re-pointed at that address. We then clear the idle sentinel +
        ``stop_reason``, rewrite the record's ``vps_ip``, and mirror it to the
        external store (a no-op for providers without one) before delegating the
        placement start to ``super()`` (whose ``_find_host_record`` reads the
        refreshed cache entry). The single mirror here keeps the offline view of a
        resumed host correct on every provider.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            raise HostNotFoundError(self.name, host_id)
        instance_id = VpsInstanceId(instance["id"])
        new_ip = self._resume_cloud_instance(instance_id)
        # The cached instance list predates the start (stale power state / IP); drop
        # it so any later discovery sees the running instance and its address.
        self._instances_cache = None
        # Rebind known_hosts to the address from mngr's local host keypairs BEFORE
        # connecting -- the instance kept its host keys across the pause (on disk),
        # but the record (the other key source) can't be read until we can SSH in.
        # A no-op for static-IP providers that override it.
        self._rebind_known_hosts_pre_connect(new_ip)
        with log_span("Waiting for VPS SSH after start"):
            self._wait_for_sshd_on_vps(new_ip, timeout_seconds=self.config.ssh_connect_timeout)
        with self._make_outer_for_vps_ip(new_ip) as outer:
            host_store = self._realizer.open_host_store(outer, host_id)
            record = host_store.read_host_record()
            if record is None or record.config is None:
                raise HostNotFoundError(self.name, host_id)
            self._rebind_known_hosts(record, new_ip)
            # Clear any stale idle sentinel so the freshly-resumed instance isn't
            # immediately re-paused by the systemd path unit (belt-and-suspenders;
            # the self-stop service also removes it when it fires).
            outer.execute_idempotent_command(f"rm -f {self._idle_sentinel_path_on_outer(host_id)}")
            certified = record.certified_host_data
            updated_data = certified.model_copy_update(
                to_update(certified.field_ref().stop_reason, None),
                to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            updated_record = record.model_copy_update(
                to_update(record.field_ref().vps_ip, new_ip),
                to_update(record.field_ref().certified_host_data, updated_data),
            )
            # Write the resumed record on-volume and mirror it to the external store
            # together, so the offline view reflects the new vps_ip and cleared
            # stop_reason (the mirror is a no-op for providers without one).
            self._write_and_mirror(host_store, updated_record)
        # Drop any cached Host bound to the old IP, then seed the record cache so
        # super().start_host()'s _find_host_record returns the rebound record.
        self._evict_cached_host(host_id)
        self._host_record_cache[host_id] = updated_record
        # The base start_host relaunches the in-container activity watcher and
        # refreshes BOOT activity on resume, so auto-stop-on-idle keeps working
        # across resumes with no provider-specific step here.
        return super().start_host(host_id, snapshot_id)

    @abstractmethod
    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        """Pause (stop / deallocate) the cloud instance -- the provider's own log span + API call."""
        ...

    @abstractmethod
    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        """Start the cloud instance and return its SSH address (a fresh IP, or the static one)."""
        ...

    def _rebind_known_hosts(self, record: VpsHostRecord, new_ip: str) -> None:
        """Re-point local known_hosts at ``new_ip`` using the instance's preserved host keys.

        A pause/resume keeps the instance's SSH host keys (on the disk), so only the
        IP changes. Drop any stale entries for the old IP, then add the new IP with
        the recorded VPS (port 22) and container host keys. Providers whose IP is
        stable across a pause override this to a no-op.
        """
        old_ip = record.vps_ip
        if old_ip is not None and old_ip != new_ip:
            remove_host_from_known_hosts(self._vps_known_hosts_path(), old_ip, 22)
            remove_host_from_known_hosts(self._container_known_hosts_path(), old_ip, self.config.container_ssh_port)
        if record.ssh_host_public_key is not None:
            add_host_to_known_hosts(
                known_hosts_path=self._vps_known_hosts_path(),
                hostname=new_ip,
                port=22,
                public_key=record.ssh_host_public_key,
            )
        if record.container_ssh_host_public_key is not None:
            add_host_to_known_hosts(
                known_hosts_path=self._container_known_hosts_path(),
                hostname=new_ip,
                port=self.config.container_ssh_port,
                public_key=record.container_ssh_host_public_key,
            )

    def _rebind_known_hosts_pre_connect(self, new_ip: str) -> None:
        """Add ``new_ip`` to known_hosts using mngr's local, authoritative host keys.

        Runs on resume *before* any SSH connection (the host record, the other key
        source, can't be read until we can connect). The VPS/container host keypairs
        are generated and held locally by mngr and injected at create time, so the
        public keys here are exactly what the resumed instance presents (its host
        keys persist on the disk across a pause). Sourcing them locally rather than
        from account-writable instance metadata anchors host-key verification to
        data mngr controls. Providers whose IP is stable across a pause override
        this to a no-op.
        """
        add_host_to_known_hosts(
            known_hosts_path=self._vps_known_hosts_path(),
            hostname=new_ip,
            port=22,
            public_key=self._get_vps_host_keypair()[1],
        )
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname=new_ip,
            port=self.config.container_ssh_port,
            public_key=self._get_container_host_keypair()[1],
        )

    def _find_instance_for_host(self, host_id: HostId) -> dict[str, Any] | None:
        """Locate this host's instance by its ``mngr-host-id`` tag/label (works while stopped), or None.

        Reads only the cached instance listing (no SSH), so it resolves an
        instance that is stopped/deallocated and therefore unreachable. The
        listing already excludes terminated/deleted instances, so a destroyed
        host returns ``None``.

        Refuses (raises) when more than one instance carries the same
        ``mngr-host-id``. The tag/label is meant to be unique but is
        account-writable, so a duplicate could otherwise silently steer ``mngr
        start`` (and the agent-tag writes keyed off this lookup) onto the wrong
        instance; failing loudly is safer than acting on an ambiguous match.
        """
        matches = self._instances_matching_host_id(host_id)
        if not matches:
            # Not in the (possibly stale) cached list. During `mngr create` the
            # cache can be populated -- e.g. by an earlier discovery/name-conflict
            # check -- before the new instance exists, so `persist_agent_data` for
            # the new agent would miss it. Refresh once and retry before giving up.
            self._instances_cache = None
            matches = self._instances_matching_host_id(host_id)
        if len(matches) > 1:
            ids = sorted(str(m.get("id")) for m in matches)
            raise MngrError(
                f"Provider {self.name!r}: {len(matches)} instances are tagged "
                f"mngr-host-id={host_id} ({', '.join(ids)}); refusing to act on an ambiguous match. "
                "Resolve the duplicate tags (or remove the stray instance) and retry."
            )
        return matches[0] if matches else None

    def _instances_matching_host_id(self, host_id: HostId) -> list[dict[str, Any]]:
        """Return every cached instance tagged ``mngr-host-id=<host_id>``.

        Providers whose tag/label values are encoded (e.g. GCE labels) override
        this to match on the encoded value.
        """
        wanted_tag = f"mngr-host-id={host_id}"
        return [instance for instance in self._list_instances_cached() if wanted_tag in instance.get("tags", ())]

    def _idle_sentinel_path_on_outer(self, host_id: HostId) -> Path:
        """Outer-filesystem path of the in-container idle sentinel for this host.

        The container writes the sentinel at ``<host_dir>/commands/<file>`` on the
        shared volume; on the outer host that maps to
        ``<btrfs_mount_path>/<host_id_hex>/host_dir/commands/<file>``.
        """
        return self._realizer.host_dir_path_on_outer(host_id) / "commands" / IDLE_SENTINEL_FILENAME

    # =========================================================================
    # Self-stopping idle watcher (in-container sentinel + host-side systemd)
    #
    # An idle container should stop the whole instance (so a paused agent costs
    # only disk), but a container cannot stop its host. Instead the in-container
    # watcher touches a sentinel on the shared volume; a host-side systemd
    # ``.path`` unit observes it and runs a oneshot ``.service`` that stops the
    # instance. The install sequence and the sentinel-touch script are shared
    # here; the ``.service`` body (poweroff vs Azure ARM deallocate) is a hook.
    # =========================================================================

    def _provider_instance_kind(self) -> str:
        """Human-readable name for this provider's machine, used only in unit ``Description=``.

        Default ``instance``; providers override with their own wording (``EC2
        instance`` / ``GCE instance`` / ``Azure VM``).
        """
        return "instance"

    def _create_shutdown_script(self, host: Host) -> None:
        """Write the idle ``shutdown.sh``: a container signals via a sentinel, bare stops the host.

        For the CONTAINER path an idle container should stop the whole instance,
        but it cannot stop its host -- so the script only touches a sentinel on the
        shared volume; a host-side systemd path unit (installed in
        ``_on_host_finalized``) observes it and stops the instance.

        For the BARE path the agent IS the VM's root, so the action depends on the
        substrate: AWS/GCP power the VM off (the realizer's ``idle_shutdown_command``,
        via ``super()._create_shutdown_script``); Azure must run an ARM deallocate
        instead, since an OS poweroff does not halt Azure compute billing. The bare
        branch is delegated to ``_write_bare_idle_shutdown_script`` so Azure can
        override it. ``self._realizer.idle_shutdown_stops_host`` is True exactly for
        the bare realizer.
        """
        if self._realizer.idle_shutdown_stops_host:
            self._write_bare_idle_shutdown_script(host)
            return
        sentinel_in_container = str(host.host_dir / "commands" / IDLE_SENTINEL_FILENAME)
        self._write_shutdown_script(host, build_sentinel_shutdown_script(sentinel_in_container))

    def _write_bare_idle_shutdown_script(self, host: Host) -> None:
        """Hook: write the BARE-placement idle ``shutdown.sh`` (default: the realizer's poweroff).

        AWS/GCP use the realizer's ``idle_shutdown_command`` (``shutdown -P now``)
        via the base ``VpsProvider._create_shutdown_script``. Azure overrides this to
        write its ARM self-deallocate script instead, because an Azure OS shutdown
        leaves the VM Stopped-but-allocated (still billing compute).
        """
        super()._create_shutdown_script(host)

    def _idle_watcher_service_unit(self, sentinel_on_outer: str) -> str:
        """Hook: the oneshot ``.service`` body the host-side idle watcher runs.

        Default (AWS/GCP) powers the host off with ``shutdown -P now`` (the poweroff
        script removes the sentinel first so a resumed instance is not immediately
        re-stopped). Azure overrides this to run its installed ARM self-deallocate
        script.
        """
        del sentinel_on_outer
        return build_poweroff_idle_watcher_service_unit()

    def _prepare_idle_watcher_outer(self, outer: OuterHostInterface, sentinel_on_outer: str) -> None:
        """Hook: provider setup on the outer before the idle-watcher units are written.

        ``sentinel_on_outer`` is the outer-filesystem path of the idle sentinel (the
        same value the units reference). Default (AWS/GCP) installs the poweroff script
        the ``.service`` runs (it removes the sentinel, then ``shutdown -P now``). Azure
        overrides this to install curl and write its ARM self-deallocate script instead
        (an Azure OS shutdown does not halt compute billing).
        """
        outer.write_text_file(
            Path(IDLE_WATCHER_POWEROFF_SCRIPT_PATH), build_poweroff_idle_watcher_script(sentinel_on_outer)
        )
        outer.execute_idempotent_command(f"chmod +x {IDLE_WATCHER_POWEROFF_SCRIPT_PATH}")

    def _install_idle_watcher(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the systemd path/service idle watcher on the outer host.

        The path unit watches the outer-filesystem location of the in-container idle
        sentinel and, when it appears, the oneshot service stops the instance (the
        action is the ``_idle_watcher_service_unit`` hook's body). Returns early
        (after a WARNING) when the host record is missing.
        """
        record = self._find_host_record(host_id)
        if record is None or record.config is None:
            logger.warning(
                "Idle watcher: no host record for {}; skipping watcher install (no auto-stop)",
                host_id,
            )
            return
        sentinel_on_outer = str(self._idle_sentinel_path_on_outer(host_id))
        with log_span("Installing idle self-stop watcher"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                self._prepare_idle_watcher_outer(outer, sentinel_on_outer)
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.path"),
                    build_idle_watcher_path_unit(sentinel_on_outer, self._provider_instance_kind()),
                )
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.service"),
                    self._idle_watcher_service_unit(sentinel_on_outer),
                )
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {IDLE_WATCHER_UNIT_NAME}.path")
        logger.info("Idle self-stop watcher installed for host {}", host_id)

    # =========================================================================
    # Host-side offline host_dir capability (select-once backend)
    #
    # Offline host_dir is a bucket feature: a provider mirrors host_dir to an object
    # store so a stopped host's host_dir is still readable. The provider selects one
    # ``HostDirBackend`` once (bucket-backed when enabled + present, else the no-op
    # ``NullHostDirBackend``), so the call sites below never re-test the feature flag
    # or bucket presence. GCP has no object store and keeps the no-op default.
    # =========================================================================

    @property
    def _host_dir_backend(self) -> HostDirBackend:
        """The offline ``host_dir`` capability: bucket-backed when enabled + present, else a no-op.

        Offline ``host_dir`` is a bucket feature, so this lives on the offline-capable
        layer (not the tag mirror). The default is the no-op ``NullHostDirBackend`` --
        correct for a provider with no bucket (e.g. GCP). Providers that mirror
        host_dir to a bucket override this with a selected-once cached property, so
        the host_dir paths below never re-test ``is_offline_host_dir_enabled`` /
        bucket presence.
        """
        return NullHostDirBackend()

    def _sync_host_dir_before_pause(self, host_id: HostId, vps_ip: str) -> None:
        """Push host_dir to the external store one final time before the instance pauses.

        Runs in ``stop_host`` after the container has stopped (host_dir quiesced)
        and before the instance is paused (still SSH-reachable), so the offline view
        is current the moment the instance stops. The no-op backend makes this a
        no-op for providers without a bucket.
        """
        self._host_dir_backend.trigger_final_sync(host_id, vps_ip)

    def get_volume_reference_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Return the bucket-backed host_dir volume *reference* (cheap, no network probe), or None.

        Delegates to the selected host_dir backend (the no-op backend returns None
        when the feature is off or no bucket exists).
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        return self._host_dir_backend.volume_reference(host_id)

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Return the bucket-backed host_dir volume, with a light existence probe, or None.

        Delegates to the selected host_dir backend, which probes that the host's
        ``host_dir/`` prefix has objects and, when empty, warns if the instance was
        never granted the bucket-write identity. Returns None when unavailable.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        return self._host_dir_backend.volume(host_id)

    # =========================================================================
    # Post-finalize provisioning (best-effort idle watcher + host_dir sync)
    # =========================================================================

    def _post_finalize_steps(self, *, host_id: HostId, vps_ip: str) -> list[tuple[str, Callable[[], None]]]:
        """Extra best-effort post-finalize steps a provider prepends to the shared list.

        Each entry is ``(failure_description, step)``; a failing step is logged at
        WARNING (with the description) and the rest still run. Default empty. Azure
        prepends its self-deallocate role assignment here.
        """
        return []

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Run the best-effort post-finalize steps after the host record is durable.

        Runs an ordered list of steps, each best-effort: a step that raises
        ``MngrError`` is logged at WARNING (with its description) and the rest still
        run, so a failure here never fails an already-durable ``create_host``.

        The shared list is: install the host-side idle watcher (skipped when the
        realizer's idle command already stops the whole host -- the bare case --
        since a bare placement self-stops directly) and install the host_dir-to-bucket
        sync daemon (a no-op when the selected ``_host_dir_backend`` is the null
        backend, e.g. GCP). Providers prepend their own steps via
        ``_post_finalize_steps`` (Azure: its self-deallocate role assignment).
        """
        steps: list[tuple[str, Callable[[], None]]] = list(self._post_finalize_steps(host_id=host_id, vps_ip=vps_ip))
        if not self._realizer.idle_shutdown_stops_host:
            steps.append(
                (
                    "the agent will not auto-stop on idle, but `mngr stop` still works",
                    lambda: self._install_idle_watcher(host_id=host_id, vps_ip=vps_ip),
                )
            )
        steps.append(
            (
                "the stopped host's host_dir will not be readable offline",
                lambda: self._host_dir_backend.install_sync(host_id=host_id, vps_ip=vps_ip),
            )
        )
        for description, step in steps:
            try:
                step()
            except MngrError as e:
                logger.warning("Post-finalize step failed for host {} ({}); {}", host_id, e, description)

    @property
    @abstractmethod
    def _state_store(self) -> HostStateStore:
        """The external host/agent-record mirror this provider reads and writes offline state through.

        Every offline-capable provider selects exactly one store (implemented as a
        cached property so any bucket-existence probe runs at most once): the
        object-storage ``BucketHostStateStore`` (AWS S3, Azure Blob), the GCP
        instance-metadata store, or ``MissingBucketHostStateStore`` when a required
        bucket has not yet been provisioned. Selecting it here lets the persist /
        remove / list / read paths below stop branching on the backing store.
        """
        ...

    @abstractmethod
    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        """Build a STOPPED ``DiscoveredHost`` from an instance's identity tags/labels/metadata.

        Returns ``None`` when the instance is not a mngr host. Raises ``ValueError``
        when the instance carries a mngr host identity that is malformed (a
        corrupt/externally-edited host-id or name). Reads only the cheap cached
        listing (host id + name), never the state store, so a discovery sweep stays
        cheap; the full record is reconstructed from the store only on demand
        (``to_offline_host``).
        """
        ...

    @abstractmethod
    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether this instance's OS is down (stopped/deallocated, and their in-flight transitions).

        Called only for mngr instances the live SSH sweep did NOT surface, so a
        provider that must spend a per-instance API call to read power state pays
        for it only on the unreachable ones.
        """
        ...

    def _offline_agent_dicts_for(self, host_id: HostId, instance: Mapping[str, Any] | None = None) -> list[dict]:
        """Return a stopped host's mirrored agent records from the external state store.

        Keyed by ``host_id``; the ``instance`` argument is accepted (the discovery
        loop passes the instance it is already iterating) but unused, since the
        store resolves everything from ``host_id``. A read against a provider whose
        required bucket is absent raises an actionable error (see
        ``MissingBucketHostStateStore``), which the discovery wrapper attributes to
        this provider and surfaces per the caller's ``--on-error``.
        """
        del instance
        return self._state_store.list_agent_records(host_id)

    def _mirror_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        """Mirror one agent record into the external state store (upsert; best-effort)."""
        self._state_store.persist_agent_record(host_id, agent_id, agent_data)

    def _remove_mirrored_agent_record(self, host_id: HostId, agent_id: str) -> None:
        """Remove one agent's mirrored record from the external state store. Idempotent, best-effort."""
        self._state_store.remove_agent_record(host_id, agent_id)

    def _persist_host_record_externally(self, record: VpsHostRecord) -> None:
        """Mirror the full host record into the external state store (best-effort)."""
        self._state_store.persist_host_record(record)

    def _delete_host_record_externally(self, host_id: HostId) -> None:
        """Delete the host's state from the external state store (best-effort, idempotent)."""
        self._state_store.delete_host_state(host_id)

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return the SSH-reachable public IPs of this provider's instances.

        Every cloud (AWS/GCP/Azure) provider reaches this point with resolvable
        credentials: ``build_provider_instance`` raises ``ProviderUnavailableError``
        when the session/subscription/credentials cannot be resolved, so the live
        listing is always available here. A stopped EC2/GCE instance loses its
        ephemeral IP and is naturally excluded by the non-empty ``main_ip`` check; a
        *deallocated* Azure VM keeps its Static IP, so it is still listed and then
        fails fast over the bounded SSH connect timeout before being reconstructed
        offline -- this keeps discovery uniform (no power-state-specific branch).
        """
        vps_ips: list[str] = []
        for instance in self._list_instances_cached():
            main_ip = instance.get("main_ip", "")
            if main_ip:
                vps_ips.append(main_ip)
        return vps_ips

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist an agent's record on the host volume *and* mirror it for offline reads.

        The base ``VpsProvider`` writes the authoritative on-volume record
        (read by the SSH-based discovery for *running* hosts), so this keeps doing
        that via ``super()``. That write is best-effort: a *stopped* host raises
        ``HostNotFoundError`` (no reachable ``vps_ip``), in which case only the
        offline mirror is written, so e.g. an offline ``mngr label`` still updates
        the record a stopped host lists from. ``_mirror_agent_record`` is the only
        per-provider step (instance tags/metadata, or an external store).
        """
        try:
            super().persist_agent_data(host_id, agent_data)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; mirroring agent data to the offline store only", host_id)
        agent_id = agent_data.get("id")
        if agent_id is None:
            logger.warning("Cannot mirror agent data without an id (name={!r})", agent_data.get("name"))
            return
        self._mirror_agent_record(host_id, str(agent_id), agent_data)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove the agent's on-volume record *and* its offline mirror.

        Mirrors ``persist_agent_data``: the base removes the authoritative on-volume
        record (best-effort -- ``HostNotFoundError`` when the host is stopped) and
        ``_remove_mirrored_agent_record`` drops the offline copy, so a destroyed
        agent stops appearing in both running- and stopped-host discovery. Both
        removals are idempotent.
        """
        try:
            super().remove_persisted_agent_data(host_id, agent_id)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; removing agent data from the offline store only", host_id)
        self._remove_mirrored_agent_record(host_id, str(agent_id))

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        """Augment the SSH-based base discovery with STOPPED instances it cannot reach.

        The base sweep reaches hosts over SSH, so a stopped instance (OS down) is
        invisible. Here we reconstruct those hosts and their agents from the
        instance listing so they still appear in ``mngr list`` and resolve for
        ``mngr start``.

        One bad instance never aborts the sweep: a malformed mngr host identity is
        logged and skipped. The offline check runs only for instances the live
        sweep did not already surface (and after the cheap not-a-mngr-host / dedup
        filters), so a healthy ``mngr list`` does no extra per-instance work and a
        running-but-transiently-unreachable instance is not misreported as STOPPED.
        """
        result = super().discover_hosts_and_agents(cg, include_destroyed=include_destroyed)
        online_host_ids = {ref.host_id for ref in result}
        for instance in self._list_instances_cached():
            try:
                host_ref = self._offline_discovered_host_from_instance(instance)
            except ValueError as e:
                logger.opt(exception=e).warning(
                    "Skipping instance {} in offline discovery: malformed mngr host identity",
                    instance.get("id"),
                )
                continue
            # Drop non-mngr instances and ones already surfaced live BEFORE the
            # offline check, since that check may cost a per-instance API call.
            if host_ref is None or host_ref.host_id in online_host_ids:
                continue
            if not self._is_instance_offline(instance):
                continue
            # An external-store provider reads agents from its store here (keyed by
            # host_id); an operational store failure propagates so the api/list
            # discovery wrapper attributes it to this provider and honors the
            # caller's --on-error (the malformed-identity data error above is the
            # only thing skipped per-instance).
            agent_refs: list[DiscoveredAgent] = []
            for agent_data in self._offline_agent_dicts_for(host_ref.host_id, instance):
                ref = validate_and_create_discovered_agent(agent_data, host_ref.host_id, self.name)
                if ref is not None:
                    agent_refs.append(ref)
            result[host_ref] = agent_refs
        return result

    def get_host(self, host: HostId | HostName) -> HostInterface:
        """Resolve a host, falling back to the instance-data offline host when stopped.

        The base reads the record over SSH, so a stopped instance raises
        ``HostNotFoundError``; ``mngr start`` calls this directly, so without the
        fallback a paused host could not be resumed by name. Only the ``HostId``
        form is recovered (the resume path passes a ``HostId``); a bare
        ``HostName`` for a stopped host still surfaces via discovery.
        """
        try:
            return super().get_host(host)
        except HostNotFoundError:
            if isinstance(host, HostId):
                return self.to_offline_host(host)
            raise

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Return an offline host, reconstructing a stopped host's full record from the external store.

        Falls back to the SSH/volume-backed base path first; if that can't find the
        host (stopped and unreachable), reconstruct the full ``VpsHostRecord`` from
        the external state store. Calls the SSH-only ``VpsProvider`` path directly so
        this override does not recurse into itself.
        """
        try:
            return VpsProvider.to_offline_host(self, host_id)
        except HostNotFoundError:
            record = self._state_store.read_host_record(host_id)
            if record is None:
                raise
            return self._create_offline_host(record)

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        """Return ``[]`` for a stopped host instead of raising.

        ``OfflineHost.get_state`` derives state via ``list_snapshots``; the base
        reads the list from the on-volume record, which is unreadable while
        stopped. These providers have no host-snapshot lifecycle, so a stopped
        host simply has none.
        """
        try:
            return super().list_snapshots(host)
        except HostNotFoundError:
            return []

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        """Return the host's persisted agent records, on-volume when reachable else offline.

        For a running host the SSH/volume-backed base reads the authoritative
        on-volume records; for a stopped host it raises ``HostNotFoundError`` and
        we fall back to the offline source (instance tags/metadata, or an external
        store for providers that have one).
        """
        try:
            return super().list_persisted_agent_data_for_host(host_id)
        except HostNotFoundError:
            return self._offline_agent_dicts_for(host_id)


class HostDirSyncInstallPlan(FrozenModel):
    """The cloud-specific pieces a host_dir sync install needs, computed once per install.

    The provider resolves these (the on-box sync-CLI install command, the host_dir
    sync ``.service`` body, and the bucket sync-target URI for the log line); the
    shared ``BucketHostDirBackend.install_sync`` skeleton then writes them to the
    outer via the identical systemd sequence. A provider returns ``None`` instead of
    a plan to skip the install entirely (e.g. Azure when the managed identity the
    sync would authenticate as is absent).
    """

    install_command: str = Field(description="Best-effort on-box install of the sync CLI (awscli / azcopy)")
    sync_command: str = Field(description="The ``aws s3 sync`` / ``azcopy sync`` command the oneshot runs")
    service_unit: str = Field(description="The oneshot host_dir-sync ``.service`` body")
    sync_target_uri: str = Field(description="The bucket sync-target URI, for the install log line")


class BucketHostDirBackend(HostDirBackend):
    """Shared offline ``host_dir`` backend for the object-storage providers (AWS S3, Azure Blob).

    Selected only when offline host_dir is on and the state bucket exists, so
    ``bucket`` is always present and no method re-tests it. Holds a back-reference
    to the provider for the SSH-to-outer / path plumbing the sync needs. The
    offline-read (``volume`` / ``volume_reference``), final-sync-before-pause, and
    sync-daemon install (the systemd ``.service``/``.timer`` write sequence) flows
    live here once; subclasses supply only the cloud-specific pieces (identity
    provisioning, the per-install plan, the missing-identity probe) and the small
    hooks (unit name, stop-action word, bucket error type).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: OfflineCapableVpsProvider
    bucket: StateBucket
    bucket_error_type: type[MngrError]

    @abstractmethod
    def _sync_unit_name(self) -> str:
        """systemd unit base name for the host_dir sync daemon (e.g. ``mngr-aws-host-dir-sync``)."""
        ...

    @abstractmethod
    def _pause_action(self) -> str:
        """The pause verb for log context: ``stop`` (AWS/EC2) or ``deallocate`` (Azure)."""
        ...

    @abstractmethod
    def _cloud_label(self) -> str:
        """Short cloud name for install log spans/lines (``AWS`` / ``Azure``)."""
        ...

    @abstractmethod
    def _build_install_plan(self, host_id: HostId) -> HostDirSyncInstallPlan | None:
        """Resolve the cloud-specific install pieces, or ``None`` to skip the install.

        Returns ``None`` (after logging) when the install cannot proceed -- e.g.
        Azure when the bucket-write managed identity the sync authenticates as is
        absent, so installing the daemon would only 403.
        """
        ...

    def install_sync(self, *, host_id: HostId, vps_ip: str) -> None:
        plan = self._build_install_plan(host_id)
        if plan is None:
            return
        sync_script = build_host_dir_sync_script(plan.sync_command)
        timer_unit = build_host_dir_sync_timer_unit(HOST_DIR_SYNC_INTERVAL_SECONDS)
        unit_name = self._sync_unit_name()
        with log_span(f"Installing {self._cloud_label()} host_dir sync daemon"):
            with self.provider._make_outer_for_vps_ip(vps_ip) as outer:
                outer.execute_idempotent_command(plan.install_command, timeout_seconds=300.0)
                outer.write_text_file(Path(HOST_DIR_SYNC_SCRIPT_PATH), sync_script)
                outer.execute_idempotent_command(f"chmod +x {HOST_DIR_SYNC_SCRIPT_PATH}")
                outer.write_text_file(Path(f"/etc/systemd/system/{unit_name}.service"), plan.service_unit)
                outer.write_text_file(Path(f"/etc/systemd/system/{unit_name}.timer"), timer_unit)
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {unit_name}.timer")
        logger.info(
            "{} host_dir sync daemon installed for host {} (target {})",
            self._cloud_label(),
            host_id,
            plan.sync_target_uri,
        )

    def volume_reference(self, host_id: HostId) -> HostVolume | None:
        return HostVolume(volume=self.bucket.volume_for_host(host_id))

    def volume(self, host_id: HostId) -> HostVolume | None:
        try:
            if not self.bucket.host_dir_prefix_has_objects(host_id):
                self._warn_if_identity_missing(host_id)
                return None
        except self.bucket_error_type as e:
            logger.warning(
                "Could not probe host_dir prefix for host {}; treating volume as unavailable: {}", host_id, e
            )
            return None
        return self.volume_reference(host_id)

    def trigger_final_sync(self, host_id: HostId, vps_ip: str) -> None:
        try:
            with log_span(f"Triggering final host_dir sync before {self._pause_action()}"):
                with self.provider._make_outer_for_vps_ip(vps_ip) as outer:
                    outer.execute_idempotent_command(
                        f"systemctl start --wait {self._sync_unit_name()}.service", timeout_seconds=300.0
                    )
        except MngrError as e:
            logger.warning(
                "Final host_dir sync before stopping host {} failed; the offline copy will be as of "
                "the last periodic sync: {}",
                host_id,
                e,
            )

    @abstractmethod
    def _warn_if_identity_missing(self, host_id: HostId) -> None:
        """Warn (best-effort) when an empty host_dir prefix is explained by a missing cloud identity."""
        ...


# Identity tags a stopped instance still carries (host id + name), read cheaply
# during discovery to label a STOPPED host; the authoritative records live in the
# external state store. The host-name value is stored as ``mngr-<host_name>``.
_HOST_NAME_TAG_PREFIX: Final[str] = "mngr-"


def normalized_tags_to_dict(instance: Mapping[str, Any]) -> dict[str, str]:
    """Turn an instance's normalized ``["key=value", ...]`` tag/label list into a dict (split on first ``=``).

    Shared by the tag/label-keyed providers (AWS EC2 tags, Azure VM tags, GCE
    labels) to read the ``mngr-*`` identity tags a stopped instance carries.
    """
    tags: dict[str, str] = {}
    for kv in instance.get("tags", ()):
        key, sep, value = kv.partition("=")
        if sep:
            tags[key] = value
    return tags


def host_name_from_tags(tags: Mapping[str, str], name_tag_key: str) -> HostName:
    """Recover the host name from the ``<name_tag_key>=mngr-<host_name>`` identity tag.

    Strips the ``mngr-`` prefix; falls back to the raw tag value, then to the
    ``mngr-host-id`` tag, when the name tag is missing/unprefixed. Used only to
    label a STOPPED host in discovery -- the authoritative name lives in the full
    record in the external state store.
    """
    name_tag = tags.get(name_tag_key, "")
    if name_tag.startswith(_HOST_NAME_TAG_PREFIX):
        return HostName(name_tag[len(_HOST_NAME_TAG_PREFIX) :])
    if name_tag:
        return HostName(name_tag)
    return HostName(tags.get("mngr-host-id", "unknown"))
