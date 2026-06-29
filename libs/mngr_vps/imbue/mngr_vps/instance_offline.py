import shlex
import tempfile
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

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import validate_and_create_discovered_agent
from imbue.mngr.interfaces.cleanup_failures import collecting_cleanup_failures
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr_vps.container_setup import download_directory_from_outer
from imbue.mngr_vps.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps.container_setup import translate_outer_concurrency_errors
from imbue.mngr_vps.docker_realizer import CONTAINER_HOST_KEY_NAME
from imbue.mngr_vps.errors import VpsError
from imbue.mngr_vps.host_state_store import BucketHostStateStore
from imbue.mngr_vps.host_state_store import HostDirBackend
from imbue.mngr_vps.host_state_store import HostStateStore
from imbue.mngr_vps.host_state_store import NullHostDirBackend
from imbue.mngr_vps.host_state_store import StateBucket
from imbue.mngr_vps.host_state_store import missing_state_bucket_error
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.instance import attempt_cloud_resource_teardown
from imbue.mngr_vps.interfaces import HostRealizer
from imbue.mngr_vps.primitives import ISOLATION_TAG_KEY
from imbue.mngr_vps.primitives import VPS_HOST_KEY_NAME
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import isolation_from_marker
from imbue.mngr_vps.primitives import read_host_public_key_with_legacy_fallback
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

# Host-side scripts the oneshot ``.service`` units run via ``ExecStart``. Installing
# the command as a script (rather than an inline ``ExecStart=/bin/sh -c '...'``) keeps
# the embedded paths out of systemd's + the shell's nested quoting.
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


class OfflineCapableVpsProvider(VpsProvider):
    """``VpsProvider`` for cloud providers whose hosts can be stopped while
    their disk persists, with host/agent records mirrored to an external
    ``HostStateStore`` (an object-storage state bucket for AWS/Azure, instance
    metadata for GCP).

    A stopped (deallocated / powered-off) instance keeps its disk but is
    SSH-unreachable, so the volume-backed base discovery and host resolution
    cannot see it. This class adds the shared "offline" recovery: it reconstructs
    such hosts (and their agents) from the provider's instance listing, and falls
    back to that listing whenever the on-volume path raises ``HostNotFoundError``.

    Subclasses (AWS/GCP/Azure) supply the per-provider instance-data hooks below.
    The agent-record write side (``persist_agent_data`` /
    ``remove_persisted_agent_data``) is shared here: both delegate uniformly to
    the provider's ``_state_store``.

    It also owns the shared cloud stop/start lifecycle: ``stop_host`` pauses the
    whole instance (so a paused agent costs only disk) and ``start_host`` resumes
    it, with the record-write + external mirror in one place. Providers supply only
    the cloud-API hooks (``_pause_cloud_instance`` / ``_resume_cloud_instance``) and
    override ``_capture_host_dir_before_pause`` / the known_hosts rebind where their
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
        # reachable: capture host_dir to the bucket now, before the pause. ``capture``
        # raises on a genuine failure (a lost connection, an rsync error, a bucket
        # write error). The pause is billing-critical -- an un-paused instance keeps
        # billing and, with the record already marked STOPPED, becomes undiscoverable
        # -- so the ``finally`` pauses first, guaranteeing a capture failure surfaces
        # (failing ``mngr stop``) without ever leaving a running instance.
        try:
            self._capture_host_dir_before_pause(host_id, host_record.vps_ip)
        finally:
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
        self._rebind_known_hosts_pre_connect(host_id, new_ip)
        with log_span("Waiting for VPS SSH after start"):
            self._wait_for_sshd_on_vps(new_ip, timeout_seconds=self.config.ssh_connect_timeout)
        # Mirror create's host-key wait on resume. Backends whose bootstrap re-runs
        # on every boot (GCP's GCE startup-script) re-install the host key and
        # ``systemctl restart ssh`` partway through boot, so sshd is briefly down
        # AND first serves a boot-generated key -- ``wait_for_sshd`` (any-key
        # handshake) can return inside that window, and the strict-checked connect
        # just below then hits a refused/mismatched port 22. Polling for the
        # expected key rides out the restart churn. A no-op for cloud-init backends
        # (AWS/Azure), which install the key pre-sshd and do not re-bootstrap on
        # resume, so it returns on the first poll. This matters most for bare
        # placement, whose agent endpoint *is* port 22.
        with log_span("Waiting for expected VPS host key after start"):
            expected_vps_host_key = read_host_public_key_with_legacy_fallback(
                self._key_dir(), host_id, VPS_HOST_KEY_NAME
            )
            if expected_vps_host_key is not None:
                self._wait_for_expected_host_key(
                    new_ip, expected_vps_host_key, timeout_seconds=self.config.ssh_connect_timeout
                )
        realizer = self._realizer_for_instance(instance)
        with self._make_outer_for_vps_ip(new_ip) as outer:
            host_store = realizer.open_host_store(outer, host_id)
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

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Destroy a host, taking a cloud-API teardown path when its instance is stopped.

        The base ``VpsProvider.destroy_host`` tears the host down over SSH using its
        host record, which works only while the instance is reachable. A STOPPED
        (deallocated / powered-off) instance -- whose disk persists but whose OS is
        down -- has no reachable address, so the base path either raises
        ``HostNotFoundError`` (no record found) or, worse, runs a doomed SSH teardown
        against a stale cached ``vps_ip`` and leaks the still-billing instance.

        So we dispatch up front on the instance's own power state (resolved from its
        ``mngr-host-id`` tag/label -- no SSH): a stopped instance goes straight to the
        offline teardown, which terminates it through the same
        ``vps_client.destroy_instance`` primitive the online path uses and deletes the
        external state (host + agent records, captured ``host_dir``). A running
        instance, or one that cannot be resolved at all, delegates to the base path;
        if that still raises ``HostNotFoundError`` (the instance is gone from the
        listing but a stale record remains), we fall back to the offline teardown,
        which is idempotent for an already-gone instance.

        Failing loudly is the point: a termination that could not be carried out
        raises a ``CleanupFailedGroup`` (non-zero exit) rather than reporting success,
        so a leaked instance can never masquerade as a clean destroy. A genuinely
        already-gone instance (absent from the listing) is idempotent success -- the
        external state is still cleaned up.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        # Dispatch on the instance's own power state, not the (possibly stale) cached
        # host record, so a stopped instance never reaches the base SSH teardown.
        instance = self._find_instance_for_host(host_id)
        if instance is not None and self._is_instance_offline(instance):
            logger.info("Host {} is stopped; destroying it via the offline path", host_id)
            self._destroy_offline_host(host_id, instance)
            return
        try:
            super().destroy_host(host)
        except HostNotFoundError:
            logger.info("Host {} is unreachable; destroying it via the offline path", host_id)
            # The base could not reach the host but the dispatch above resolved no
            # *offline* instance; pass the (possibly None) instance through so the
            # offline teardown does not re-list and an absent instance is idempotent.
            self._destroy_offline_host(host_id, instance)

    def _destroy_offline_host(self, host_id: HostId, instance: dict[str, Any] | None) -> None:
        """Terminate a stopped host's instance via the cloud API and delete its external state.

        ``instance`` is the host's instance as already resolved by the caller from the
        cheap tag/label listing (``None`` when it is absent -- already terminated --
        which the listing excludes). Terminates through ``vps_client.destroy_instance``
        and records a real failure -- raised as a ``CleanupFailedGroup`` -- when the
        instance exists but could not be terminated, so the still-billing instance is
        never reported as gone. The per-host provider SSH key (recovered from the
        mirrored record) and the external state (host + agent records, captured
        ``host_dir``) are cleaned up last, including in the already-gone case.
        """
        self._evict_cached_host(host_id)
        with collecting_cleanup_failures() as failures:
            # An instance still present in the listing must be terminated; an absent
            # one is already gone (benign) and only its external state is cleaned up.
            if instance is not None:
                instance_id = VpsInstanceId(instance["id"])
                with log_span("Terminating stopped cloud instance {}", instance_id):
                    attempt_cloud_resource_teardown(
                        lambda: self.vps_client.destroy_instance(instance_id),
                        resource_description=f"stopped instance {instance_id}",
                        host_id=host_id,
                        failures=failures,
                    )
            else:
                logger.debug(
                    "No instance found for stopped host {}; treating its termination as already done", host_id
                )

            # Clean up the per-host provider SSH key (the same teardown step the online
            # destroy runs). The id is recovered from the mirrored record; it is not
            # required to terminate the instance, so this runs after the terminate
            # above. An "already gone" (404/410) response is benign; any other error
            # means a key that may still be registered.
            mirrored_record = self._state_store.read_host_record(host_id)
            ssh_key_id = mirrored_record.config.vps_ssh_key_id if mirrored_record and mirrored_record.config else None
            if ssh_key_id is not None:
                attempt_cloud_resource_teardown(
                    lambda: self.vps_client.delete_ssh_key(ssh_key_id),
                    resource_description=f"SSH key {ssh_key_id}",
                    host_id=host_id,
                    failures=failures,
                )

            # Delete the external mirror last, so the host stops appearing in offline
            # listings only once the instance is actually gone (or was already gone).
            # The store removal is idempotent and tolerates an absent record; a storage
            # error propagates so a failed cleanup is not silently dropped.
            self._delete_host_record_externally(host_id)
            logger.info("Stopped host {} destroyed via the offline path", host_id)

    @abstractmethod
    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        """Pause (stop / deallocate) the cloud instance -- the provider's own log span + API call."""
        ...

    @abstractmethod
    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        """Start the cloud instance and return its SSH address (a fresh IP, or the static one)."""
        ...

    def _add_known_hosts_for_ip(
        self, ip: str, *, vps_public_key: str | None, container_public_key: str | None
    ) -> None:
        """Add ``ip`` to the VPS (port 22) and container known_hosts with the given host keys.

        The shared add half of the resume known_hosts rebind: each endpoint is added
        only when its public key is present, so a caller with a key from one side
        only (e.g. a record missing the container key) skips the absent one. Both
        rebind paths -- ``_rebind_known_hosts`` (record-sourced keys) and
        ``_rebind_known_hosts_pre_connect`` (locally-held keys) -- go through here.
        """
        if vps_public_key is not None:
            add_host_to_known_hosts(
                known_hosts_path=self._vps_known_hosts_path(),
                hostname=ip,
                port=22,
                public_key=vps_public_key,
            )
        if container_public_key is not None:
            add_host_to_known_hosts(
                known_hosts_path=self._container_known_hosts_path(),
                hostname=ip,
                port=self.config.container_ssh_port,
                public_key=container_public_key,
            )

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
        self._add_known_hosts_for_ip(
            new_ip,
            vps_public_key=record.ssh_host_public_key,
            container_public_key=record.container_ssh_host_public_key,
        )

    def _rebind_known_hosts_pre_connect(self, host_id: HostId, new_ip: str) -> None:
        """Add ``new_ip`` to known_hosts using mngr's local, authoritative host keys.

        Runs on resume *before* any SSH connection (the host record, the other key
        source, can't be read until we can connect). The VPS/container host keypairs
        are generated and held locally by mngr and injected at create time, so the
        public keys here are exactly what the resumed instance presents (its host
        keys persist on the disk across a pause). Sourcing them locally rather than
        from account-writable instance metadata anchors host-key verification to
        data mngr controls. Per-host keys are read for ``host_id``, falling back to
        the legacy provider-global key for hosts created before per-host keys
        existed. Providers whose IP is stable across a pause override this to a
        no-op.
        """
        self._add_known_hosts_for_ip(
            new_ip,
            vps_public_key=read_host_public_key_with_legacy_fallback(self._key_dir(), host_id, VPS_HOST_KEY_NAME),
            container_public_key=read_host_public_key_with_legacy_fallback(
                self._key_dir(), host_id, CONTAINER_HOST_KEY_NAME
            ),
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

    def _isolation_marker_for_instance(self, instance: Mapping[str, Any]) -> str | None:
        """Read the instance's ``mngr-isolation`` placement marker (no SSH), or None.

        Reads from the normalized ``key=value`` tag list (AWS EC2 tags, Azure VM
        tags). GCP, whose identity lives in instance metadata, overrides this to
        read the metadata item instead.
        """
        return normalized_tags_to_dict(instance).get(ISOLATION_TAG_KEY)

    def _realizer_for_instance(self, instance: Mapping[str, Any]) -> HostRealizer:
        """The realizer matching a host's placement, read from its instance marker (no SSH)."""
        marker_value = self._isolation_marker_for_instance(instance)
        # ``isolation_from_marker`` raises a bare ``ValueError`` on a corrupt marker
        # (the marker is an account-writable tag/metadata item). Wrap it as a
        # ``VpsError`` so discovery's per-VPS ``except MngrError`` degrades just that
        # host instead of aborting the whole sweep.
        try:
            isolation = isolation_from_marker(marker_value)
        except ValueError as e:
            raise VpsError(f"Corrupt {ISOLATION_TAG_KEY} marker {marker_value!r} on instance") from e
        return self._realizer_for_isolation(isolation)

    def _realizer_for_host_id(self, host_id: HostId) -> HostRealizer:
        """Resolve a host's realizer from its instance's ``mngr-isolation`` marker.

        Falls back to the create-time realizer when the host's instance is not in
        the cached listing (e.g. a freshly-created host whose listing predates it);
        an absent marker on a found instance defaults to container (see
        ``isolation_from_marker``).
        """
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            return self._realizer
        return self._realizer_for_instance(instance)

    def _realizer_for_vps_ip(self, vps_ip: str) -> HostRealizer:
        """Probe ``vps_ip``'s host with the realizer matching its instance's placement marker.

        Overrides the base (which returns the create-time realizer) so discovery
        finds a bare host even under a default-container config: the instance's
        ``mngr-isolation`` marker is read from the cached listing keyed by IP, with
        no SSH. An IP not in the listing falls back to the create-time realizer.
        """
        for instance in self._list_instances_cached():
            if instance.get("main_ip", "") == vps_ip:
                return self._realizer_for_instance(instance)
        return self._realizer

    def _idle_sentinel_path_on_outer(self, host_id: HostId) -> Path:
        """Outer-filesystem path of the in-container idle sentinel for this host.

        The container writes the sentinel at ``<host_dir>/commands/<file>`` on the
        shared volume; on the outer host that maps to
        ``<btrfs_mount_path>/<host_id_hex>/host_dir/commands/<file>``.
        """
        return self._host_dir_path_on_outer(host_id) / "commands" / IDLE_SENTINEL_FILENAME

    def _host_dir_path_on_outer(self, host_id: HostId) -> Path:
        """Outer-filesystem path of this host's ``host_dir`` tree (delegates to the realizer).

        Used by ``BucketHostDirBackend.capture`` to read the host's ``host_dir`` off
        the box at ``mngr stop``, and by ``_idle_sentinel_path_on_outer``. Resolves
        the host's own placement realizer (bare and container lay out ``host_dir``
        at different outer paths) from its instance marker.
        """
        return self._realizer_for_host_id(host_id).host_dir_path_on_outer(host_id)

    def _pull_host_dir_to_local(self, host_id: HostId, vps_ip: str, local_dir: Path) -> None:
        """Rsync the host's ``host_dir`` off the box into ``local_dir`` (operator-driven capture).

        Opens the operator's outer SSH connection and rsyncs the host_dir tree
        down. rsync copies the regular-file tree and natively skips sockets / other
        special files, so a live tmux socket can't sink the capture. A connection
        or rsync failure raises (as a ``MngrError``).
        """
        host_dir_on_outer = self._host_dir_path_on_outer(host_id)
        cg = ConcurrencyGroup(name="rsync-host-dir-capture")
        with (
            translate_outer_concurrency_errors("capture host_dir off the host"),
            self._make_outer_for_vps_ip(vps_ip) as outer,
            cg,
        ):
            download_directory_from_outer(outer, cg, str(host_dir_on_outer), local_dir)

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

    def _idle_watcher_service_unit(self) -> str:
        """Hook: the oneshot ``.service`` body the host-side idle watcher runs.

        Default (AWS/GCP) powers the host off with ``shutdown -P now`` (the poweroff
        script removes the sentinel first so a resumed instance is not immediately
        re-stopped). Azure overrides this to run its installed ARM self-deallocate
        script. The sentinel removal lives in those scripts, not the unit body, so
        this needs no sentinel path.
        """
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
        action is the ``_idle_watcher_service_unit`` hook's body). The caller
        (``_on_host_finalized``) asserts the host record is durable first, since a
        host that can never auto-stop is a create failure, not a tolerable one.
        """
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
                    self._idle_watcher_service_unit(),
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

        Offline ``host_dir`` is a bucket feature, so it lives on this
        offline-capable layer. The default is the no-op ``NullHostDirBackend`` --
        correct for a provider with no bucket (e.g. GCP). Providers that mirror
        host_dir to a bucket override this with a selected-once cached property, so
        the host_dir paths below never re-test ``is_offline_host_dir_enabled`` /
        bucket presence.
        """
        return NullHostDirBackend()

    def _select_bucket_store(
        self, bucket: StateBucket | None, *, store_label: str, prepare_command: str
    ) -> HostStateStore:
        """Build the ``BucketHostStateStore`` for ``bucket``, or raise when it is absent.

        Shared by the object-storage providers (AWS S3, Azure Blob): their
        ``_state_store`` cached property resolves its own bucket type and delegates
        here. The bucket is required -- when ``None`` (not yet provisioned), this
        raises the actionable ``missing_state_bucket_error`` pointing at
        ``prepare_command`` -- so every persist / remove / list / read fails loudly
        and uniformly. ``store_label`` names the store in errors (e.g. "S3 state
        bucket"). GCP overrides ``_state_store`` with its metadata store and never
        calls this.
        """
        if bucket is None:
            raise missing_state_bucket_error(store_label, prepare_command)
        return BucketHostStateStore(bucket=bucket, bucket_label=store_label)

    def _select_bucket_host_dir_backend(self, bucket: StateBucket | None, *, enabled: bool) -> HostDirBackend:
        """Select the offline ``host_dir`` backend once: bucket-backed when enabled + present, else no-op.

        Shared by the object-storage providers: the only place the feature flag and
        bucket presence are tested together, so every host_dir call site dispatches
        through the selected backend instead of re-deriving the condition. ``enabled``
        is the provider config's ``is_offline_host_dir_enabled``.
        """
        if enabled and bucket is not None:
            return BucketHostDirBackend(provider=self, bucket=bucket)
        return NullHostDirBackend()

    def _capture_host_dir_before_pause(self, host_id: HostId, vps_ip: str) -> None:
        """Capture host_dir to the bucket before the instance pauses (operator-driven).

        Runs in ``stop_host`` after the container has stopped (host_dir quiesced)
        and before the instance is paused (still SSH-reachable), so the operator
        reads the final host_dir off the box and uploads it -- making the offline
        view current the moment the instance stops. The no-op backend makes this a
        no-op for providers without a bucket. Best-effort (see ``capture``): a
        failure never breaks ``stop_host``.
        """
        self._host_dir_backend.capture(host_id, vps_ip)

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
        ``host_dir/`` prefix has objects (something was captured at ``mngr stop``).
        Returns None when nothing was captured.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        return self._host_dir_backend.volume(host_id)

    # =========================================================================
    # Post-finalize provisioning (best-effort idle watcher)
    # =========================================================================

    def _post_finalize_steps(self, *, host_id: HostId, vps_ip: str) -> list[tuple[str, Callable[[], None]]]:
        """Extra best-effort post-finalize steps a provider prepends to the shared list.

        Each entry is ``(failure_description, step)``; a failing step is logged at
        WARNING (with the description) and the rest still run. Default empty. Azure
        prepends its self-deallocate role assignment here.
        """
        return []

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Run the post-finalize provisioning after the host record is durable.

        Two tiers. The *best-effort* steps -- any provider-supplied
        ``_post_finalize_steps`` (Azure: its self-deallocate role assignment) and
        the host-side idle watcher (skipped when the realizer's idle command already
        stops the whole host -- the bare case -- since a bare placement self-stops
        directly) -- are each logged at WARNING and tolerated: a failure there never
        fails an already-durable ``create_host`` (the agent simply won't auto-stop
        on idle, but ``mngr stop`` still works).

        The one exception is a *missing host record* when the idle watcher is due to
        be installed: this method runs only after the record is durable, so a missing
        record is a broken invariant, not a tolerable install failure. It raises
        (failing ``create_host``, whose cleanup then tears the VPS back down) rather
        than silently shipping a host that can never auto-stop.

        Offline host_dir needs no create-time provisioning: it is captured
        operator-side at ``mngr stop`` (see ``_capture_host_dir_before_pause``), so
        there is no sync daemon to install and no bucket-write identity to attach.
        """
        steps: list[tuple[str, Callable[[], None]]] = list(self._post_finalize_steps(host_id=host_id, vps_ip=vps_ip))
        if not self._realizer.idle_shutdown_stops_host:
            # The idle watcher is this host's only auto-stop safety net. We run after
            # the record is durable, so a missing record is a broken invariant: fail
            # loudly here (outside the best-effort loop below) rather than skip the
            # watcher and silently ship a host that can never auto-stop on idle.
            record = self._find_host_record(host_id)
            if record is None or record.config is None:
                raise HostCreationError(
                    self.name,
                    f"Host record for {host_id} vanished immediately after finalize; "
                    "cannot install the idle auto-stop watcher.",
                )
            steps.append(
                (
                    "the agent will not auto-stop on idle, but `mngr stop` still works",
                    lambda: self._install_idle_watcher(host_id=host_id, vps_ip=vps_ip),
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
        object-storage ``BucketHostStateStore`` (AWS S3, Azure Blob) or the GCP
        instance-metadata store. When a required object-storage bucket has not yet
        been provisioned the property raises an actionable error (see
        ``missing_state_bucket_error``) rather than returning a degraded store, so
        every persist / remove / list / read below fails loudly and uniformly.
        Selecting the store here lets those paths stop branching on the backing
        store.
        """
        ...

    def _validate_external_store_ready(self) -> None:
        """Fail fast (before launch) when the required offline state store is absent.

        Accessing ``_state_store`` raises an actionable "run prepare" error when a
        provider's required object-storage bucket has not been provisioned
        (AWS/Azure); for the metadata-backed store (GCP) it is a cheap
        construction. Probing here -- before the SSH key upload and instance launch
        in ``create_host`` -- means a missing bucket fails ``mngr create`` cleanly
        instead of after the instance is already running.
        """
        _ = self._state_store

    def _host_name_tag_key(self) -> str:
        """The instance tag/label key holding the host name (as ``mngr-<host_name>``).

        Default is ``Name`` (the AWS EC2 ``Name`` tag); Azure overrides it with its
        own host-name tag key. Read by the shared
        ``_offline_discovered_host_from_instance`` to label a STOPPED host. GCP
        overrides ``_offline_discovered_host_from_instance`` itself (its identity is
        metadata-encoded), so this hook is unused there.
        """
        return "Name"

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        """Build a STOPPED ``DiscoveredHost`` from an instance's identity tags/labels/metadata.

        The shared default reads the cheap ``mngr-*`` identity tags a stopped
        instance still carries (host id + the ``_host_name_tag_key()`` name tag) from
        the normalized tag list -- never the state store -- so a discovery sweep
        stays cheap; the full record is reconstructed from the store only on demand
        (``to_offline_host``). Returns ``None`` when the instance is not a mngr host.
        Raises ``ValueError`` when the instance carries a mngr host identity that is
        malformed (a corrupt/externally-edited host-id or name). GCP overrides this
        to read its metadata-encoded identity instead.
        """
        tags = normalized_tags_to_dict(instance)
        host_id_str = tags.get("mngr-host-id")
        if host_id_str is None:
            return None
        return DiscoveredHost(
            host_id=HostId(host_id_str),
            host_name=host_name_from_tags(tags, self._host_name_tag_key()),
            provider_name=self.name,
            host_state=HostState.STOPPED,
        )

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
        required bucket is absent (or a bucket storage error) raises, which the
        discovery wrapper attributes to this provider and surfaces per the caller's
        ``--on-error``.
        """
        del instance
        return self._state_store.list_agent_records(host_id)

    def _mirror_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        """Mirror one agent record into the external state store (upsert).

        Propagates a storage/missing-bucket error: the bucket is required, so a
        dropped mirror would let a stopped host show stale agents.
        """
        self._state_store.persist_agent_record(host_id, agent_id, agent_data)

    def _remove_mirrored_agent_record(self, host_id: HostId, agent_id: str) -> None:
        """Remove one agent's mirrored record from the external state store (idempotent; errors propagate)."""
        self._state_store.remove_agent_record(host_id, agent_id)

    def _persist_host_record_externally(self, record: VpsHostRecord) -> None:
        """Mirror the full host record into the external state store (errors propagate)."""
        self._state_store.persist_host_record(record)

    def _delete_host_record_externally(self, host_id: HostId) -> None:
        """Delete the host's state from the external state store (idempotent; errors propagate)."""
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
        the record a stopped host lists from. ``_mirror_agent_record`` writes the
        agent record to the provider's external ``_state_store`` (an object-storage
        bucket for AWS/Azure, instance metadata for GCP).
        """
        try:
            super().persist_agent_data(host_id, agent_data)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; mirroring agent data to the offline store only", host_id)
        # Warn-not-raise, matching the on-volume writer this mirrors (host_store.py
        # "Cannot persist agent data without id field") and the Modal provider
        # (mngr_modal/instance.py, same guard): an id-less record can't be keyed, so
        # both halves of the write skip it uniformly rather than one raising.
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
        logged and skipped. This matches the live path, which warn-and-skips a
        corrupt on-volume record identically (``host_store.py`` "Failed to parse host
        record" returns None), and the Modal provider (mngr_modal/instance.py
        "Skipped sandbox with invalid tags"). Per-instance data corruption is skipped
        here; only *provider*-level operational failures propagate, where the api/list
        wrapper honors the caller's ``--on-error`` (a provider-granular control). The
        offline check runs only for instances the live sweep did not already surface
        (and after the cheap not-a-mngr-host / dedup filters), so a healthy ``mngr
        list`` does no extra per-instance work and a running-but-transiently-
        unreachable instance is not misreported as STOPPED.
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
        we fall back to the offline source: the provider's external ``_state_store``
        (an object-storage bucket for AWS/Azure, instance metadata for GCP).
        """
        try:
            return super().list_persisted_agent_data_for_host(host_id)
        except HostNotFoundError:
            return self._offline_agent_dicts_for(host_id)


def _read_local_file_tree(root: Path) -> dict[str, bytes]:
    """Read every regular file under ``root`` into a ``{posix-relpath: bytes}`` map.

    Used after rsyncing a captured ``host_dir`` into a local temp dir: walks the
    tree and reads regular files only, skipping symlinks and any special files
    rsync may have left behind. Keys are POSIX-relative to ``root``.
    """
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


# A captured host_dir can be a full git checkout -- thousands of tiny files --
# and an object-store volume PUTs one object per file. Uploading the files across
# this many worker threads overlaps the per-object round-trips instead of
# serializing them (which made `mngr stop` on a large host_dir take minutes).
_HOST_DIR_UPLOAD_CONCURRENCY: Final[int] = 32


def _write_files_concurrently(volume: Volume, files: Mapping[str, bytes]) -> None:
    """Upload ``files`` to ``volume`` with the per-object writes overlapped across worker threads.

    Object-store volumes PUT one object per file, so a large captured host_dir
    otherwise uploads file-by-file, serializing one WAN round-trip per object.
    The files are split round-robin across a bounded pool of workers, each of
    which writes its share through the volume's public ``write_files`` (so the
    volume's own error translation is unchanged). A failure in any worker
    surfaces via ``future.result()``.
    """
    items = list(files.items())
    if not items:
        return
    worker_count = min(_HOST_DIR_UPLOAD_CONCURRENCY, len(items))
    chunks: list[dict[str, bytes]] = [{} for _ in range(worker_count)]
    for index, (path, content) in enumerate(items):
        chunks[index % worker_count][path] = content
    with ConcurrencyGroup(name="host-dir-capture-upload") as cg:
        with ConcurrencyGroupExecutor(
            parent_cg=cg, name="host-dir-capture-upload", max_workers=worker_count
        ) as executor:
            futures = [executor.submit(volume.write_files, chunk) for chunk in chunks]
    # Surface a worker's failure *outside* the ConcurrencyGroup block: re-raising it
    # inside would let the group's __exit__ wrap it in a ConcurrencyExceptionGroup,
    # hiding the underlying MngrError that the caller's error handling expects.
    for future in futures:
        future.result()


class BucketHostDirBackend(HostDirBackend):
    """Operator-driven offline ``host_dir`` backend for the object-storage providers (AWS S3, Azure Blob).

    Selected only when offline host_dir is on and the state bucket exists, so
    ``bucket`` is always present and no method re-tests it. Cloud-agnostic and
    concrete: capture reads the host's ``host_dir`` off the box over the generic
    outer-host interface and uploads it to the bucket with the operator's
    credentials, and the read path serves it back -- so there is no per-cloud
    subclass, no instance/managed identity, and no on-box sync daemon. Holds a
    back-reference to the provider for the SSH-to-outer / host_dir-path plumbing.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: OfflineCapableVpsProvider
    bucket: StateBucket

    def volume_reference(self, host_id: HostId) -> HostVolume | None:
        return HostVolume(volume=self.bucket.volume_for_host(host_id))

    def volume(self, host_id: HostId) -> HostVolume | None:
        # An empty host_dir prefix means nothing was captured yet (the host was
        # never `mngr stop`-ped, or idle-self-poweroffed with no operator to
        # capture it) -> no offline volume. A bucket probe error propagates.
        if not self.bucket.host_dir_prefix_has_objects(host_id):
            logger.debug("No offline host_dir captured for host {} (its host_dir prefix is empty)", host_id)
            return None
        return self.volume_reference(host_id)

    def capture(self, host_id: HostId, vps_ip: str) -> None:
        """Pull the host's ``host_dir`` off the box and upload it to the bucket (operator-driven).

        rsyncs the host_dir tree off the box into a local temp dir (via the
        provider's outer SSH connection), then writes every captured file into the
        bucket's host_dir volume with the operator's credentials. rsync handles the
        whole tree and skips sockets / other special files natively, so a live tmux
        socket can't sink the capture. A genuine failure -- a lost connection, an
        rsync error, or a bucket write error -- raises (with host_dir context)
        rather than being swallowed, so the operator knows the offline copy was not
        captured; an empty host_dir is captured as nothing (no error). The caller
        (``stop_host``) pauses the instance in a ``finally`` *before* this can
        propagate, so raising never leaks a running instance: the host is stopped
        and ``mngr stop`` then surfaces the error. The tree is read into memory
        and the per-file uploads are overlapped across worker threads
        (``_write_files_concurrently``), so a large host_dir (a full git checkout)
        does not serialize one object-store round-trip per file.
        """
        try:
            with log_span("Capturing host_dir to the bucket for host {}", host_id):
                with tempfile.TemporaryDirectory(prefix="mngr-host-dir-capture-") as tmp:
                    local_root = Path(tmp)
                    self.provider._pull_host_dir_to_local(host_id, vps_ip, local_root)
                    files = _read_local_file_tree(local_root)
                    if files:
                        _write_files_concurrently(self.bucket.volume_for_host(host_id), files)
                    else:
                        logger.debug("host_dir for host {} is empty; nothing to capture", host_id)
        # A capture failure surfaces (it is NOT swallowed) -- the operator should
        # know the offline host_dir was not captured. stop_host's ``finally``
        # guarantees the instance is paused *before* this propagates, so raising
        # can never leak a running instance: the host is stopped and `mngr stop`
        # then reports the failure. The expected failure modes (a connection drop,
        # an rsync failure, or a bucket storage error) are re-raised with host_dir
        # context; an unexpected error propagates as-is.
        except (HostConnectionError, MngrError, OSError) as e:
            raise MngrError(
                f"Failed to capture host_dir to the bucket for host {host_id}: {e}. The host is stopped, but "
                "its offline host_dir was not captured, so `mngr event` / `mngr file` on it will be unavailable "
                "or stale until the next successful stop."
            ) from e


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


def host_name_from_prefixed_value(raw_name: str, host_id_fallback: str) -> HostName:
    """Recover a host name from a ``mngr-<host_name>`` identity value.

    Strips the ``mngr-`` prefix; falls back to the raw value, then the host id, then
    ``"unknown"``. Shared by the tag-keyed providers (``host_name_from_tags``) and
    GCP's metadata-keyed recovery, which read the same ``mngr-``-prefixed value from
    different sources (instance tags vs GCE metadata). Used only to label a STOPPED
    host in discovery -- the authoritative name lives in the external state record.
    """
    if raw_name.startswith(_HOST_NAME_TAG_PREFIX):
        return HostName(raw_name[len(_HOST_NAME_TAG_PREFIX) :])
    return HostName(raw_name or host_id_fallback or "unknown")


def host_name_from_tags(tags: Mapping[str, str], name_tag_key: str) -> HostName:
    """Recover the host name from the ``<name_tag_key>=mngr-<host_name>`` identity tag."""
    return host_name_from_prefixed_value(tags.get(name_tag_key, ""), tags.get("mngr-host-id", ""))
