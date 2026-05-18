"""ImbueCloudProvider: discover, destroy, delete leased pool hosts.

Lease creation is intentionally NOT done as part of `mngr create --provider
imbue_cloud_<account>`. Users go through `mngr imbue_cloud claim` (which is
the analogue of today's minds LEASED flow consolidated into the plugin).
That command produces a lease, registers the host with the connector, and
runs the rename + label + env-injection sequence in 2 SSH round trips.

This provider's responsibilities are then:
- `discover_hosts` -- list this account's leased hosts via the connector.
- `get_host` -- build a Host pointing at the leased VPS:container_ssh_port.
- `destroy_host` -- stop the docker container on the VPS via SSH; lease and
  on-disk data are preserved.
- `delete_host` -- call /hosts/{id}/release and drop on-disk plugin state.
- `start_host` -- start the docker container on the VPS.
- `stop_host` -- stop the docker container on the VPS.
"""

import json
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
from typing import Final

import paramiko
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import compute_idle_seconds
from imbue.mngr.hosts.common import determine_lifecycle_state
from imbue.mngr.hosts.common import resolve_expected_process_name
from imbue.mngr.hosts.common import timestamp_to_datetime
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
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
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.provider_instance import build_agent_details_from_offline_ref
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.listing_utils import build_outer_listing_collection_script
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_imbue_cloud.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_provider_data_dir
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.host import ImbueCloudHost
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore

_SSH_WAIT_TIMEOUT_SECONDS: Final[float] = 120.0


def _certified_host_name(raw: Mapping[str, Any]) -> str | None:
    """Pull the friendly host name out of the listing raw output, if present."""
    certified = raw.get("certified_data") or {}
    name = certified.get("host_name")
    return name if isinstance(name, str) and name else None


def _derive_host_state_from_raw(raw: Mapping[str, Any]) -> HostState:
    """Map the outer-listing raw output to a HostState.

    The outer listing script tags the output with ``CONTAINER_STATE``,
    ``CONTAINER_EXIT_CODE``, and ``CONTAINER_MISSING`` so we don't have
    to re-run docker inspect.
    """
    if raw.get("container_missing"):
        return HostState.DESTROYED
    container_state = raw.get("container_state")
    if not container_state:
        # Outer SSH succeeded but produced no state -- treat as crashed
        # (no info to be more specific).
        return HostState.CRASHED
    exit_code = raw.get("container_exit_code") or 0
    has_certified_data = bool(raw.get("certified_data"))
    if container_state == "running" and has_certified_data:
        return HostState.RUNNING
    if container_state == "running":
        # Container is up but docker exec didn't give us data -- we know
        # the host exists but can't read its state from inside.
        return HostState.UNAUTHENTICATED
    state, _note = _map_docker_status_to_host_state(container_state, exit_code)
    return state


def _derive_offline_note_from_raw(raw: Mapping[str, Any]) -> str | None:
    """Produce a short ``failure_reason`` note for non-running containers.

    Returns None for running containers (no note needed) and for the
    DESTROYED / missing case (the state itself is the message). For
    stopped/paused/etc., returns the human-readable note that
    ``_map_docker_status_to_host_state`` produced.
    """
    container_state = raw.get("container_state")
    if not container_state or container_state == "running":
        return None
    if raw.get("container_missing"):
        return None
    exit_code = raw.get("container_exit_code") or 0
    _state, note = _map_docker_status_to_host_state(container_state, exit_code)
    return note


def _map_docker_status_to_host_state(status: str, exit_code: int) -> tuple[HostState, str | None]:
    """Translate docker's container ``State.Status`` into a ``HostState``.

    Returns ``(state, note)`` where ``note`` is a short human-readable
    diagnostic appended to ``HostDetails.failure_reason``. If the docker
    container is ``running`` but inner SSH was unreachable we treat that
    as an authentication problem -- the host is up; we just can't get
    inside it.
    """
    if status == "running":
        return HostState.UNAUTHENTICATED, "container is running on outer host but inner SSH was unreachable"
    if status == "exited":
        if exit_code == 0:
            return HostState.STOPPED, "container exited cleanly"
        return HostState.CRASHED, f"container exited with code {exit_code}"
    if status == "paused":
        return HostState.PAUSED, "container is paused"
    if status in ("created", "restarting"):
        return HostState.STARTING, f"container in {status} state"
    if status in ("dead", "removing"):
        return HostState.CRASHED, f"container in {status} state"
    return HostState.CRASHED, f"unrecognized docker status {status!r}"


def _scan_ssh_host_key(host: str, port: int) -> str | None:
    """Best-effort: pull a remote sshd's public key for known_hosts.

    Used for both the inner container's sshd (port 2222) and the outer
    VPS root sshd (port 22). Returns ``"<key_type> <base64>"`` on success,
    or ``None`` on any failure (timeout, connection refused, protocol
    error). Callers add this to ``known_hosts`` so subsequent SSH
    connections succeed under ``StrictHostKeyChecking``.
    """
    transport = paramiko.Transport((host, port))
    try:
        transport.start_client(timeout=10.0)
        host_key = transport.get_remote_server_key()
    except (paramiko.SSHException, OSError):
        return None
    finally:
        try:
            transport.close()
        except (OSError, paramiko.SSHException):
            pass
    return f"{host_key.get_name()} {host_key.get_base64()}"


class ImbueCloudProvider(BaseProviderInstance):
    """Provider that surfaces a single account's imbue-cloud leases as mngr hosts."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: ImbueCloudProviderConfig = Field(frozen=True, description="Configuration for this provider instance")
    client: ImbueCloudConnectorClient = Field(frozen=True, description="HTTP client for the connector")
    session_store: ImbueCloudSessionStore = Field(frozen=True, description="Shared session store keyed by user_id")

    _leased_hosts_cache: list[LeasedHostInfo] | None = PrivateAttr(default=None)
    # Parsed listing output keyed by host_id; populated by
    # ``discover_hosts_and_agents`` via outer SSH + ``docker exec`` (running)
    # or ``docker cp`` (stopped), and consumed by ``get_host_and_agent_details``
    # so the two listing phases share a single outer-SSH round-trip per host.
    _listing_raw_cache: dict[HostId, dict[str, Any]] = PrivateAttr(default_factory=dict)

    # ------------------------------------------------------------------
    # Capability flags
    # ------------------------------------------------------------------

    @property
    def supports_snapshots(self) -> bool:
        return False

    @property
    def supports_shutdown_hosts(self) -> bool:
        return True

    @property
    def supports_volumes(self) -> bool:
        return False

    @property
    def supports_mutable_tags(self) -> bool:
        return False

    def reset_caches(self) -> None:
        super().reset_caches()
        self._leased_hosts_cache = None
        self._listing_raw_cache.clear()

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _provider_data_dir(self) -> Path:
        return get_provider_data_dir(self.mngr_ctx.profile_dir, str(self.name))

    def _host_state_dir(self, host_id: HostId) -> Path:
        return self._provider_data_dir() / "hosts" / str(host_id)

    def _host_keypair_paths(self, host_id: HostId) -> tuple[Path, Path]:
        host_dir = self._host_state_dir(host_id)
        host_dir.mkdir(parents=True, exist_ok=True)
        return host_dir / "ssh_key", host_dir / "ssh_key.pub"

    def _host_known_hosts_path(self, host_id: HostId) -> Path:
        return self._host_state_dir(host_id) / "known_hosts"

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _resolve_account(self, override: str | None = None) -> ImbueCloudAccount | None:
        """Pick the effective account for this provider operation.

        Precedence: explicit ``override`` > ``self.config.account`` >
        active-account marker on disk (set by ``mngr imbue_cloud auth use``
        / ``signin``). Returns ``None`` when none of the above produce an
        account; callers are responsible for raising a useful error in
        that case (the right message depends on what they were trying to
        do).
        """
        if override:
            return ImbueCloudAccount(override)
        if self.config.account is not None:
            return self.config.account
        return self.session_store.get_active_account()

    def _get_access_token(self, account: ImbueCloudAccount) -> SecretStr:
        """Fetch a fresh access token for ``account``.

        Wrapping the call in a method makes the access path easy to mock in
        tests and keeps the refresh-on-near-expiry policy in one place.
        """
        return get_active_token(self.session_store, self.client, account)

    def _require_account(self, override: str | None = None) -> ImbueCloudAccount:
        """Like ``_resolve_account`` but raises if no account is available.

        Use this from any code path that genuinely needs to talk to the
        connector (including ``discover_hosts`` -- if a provider instance
        is enabled it must produce a usable account). The error message
        names the active-account knobs so the caller knows exactly what to
        do.
        """
        resolved = self._resolve_account(override)
        if resolved is not None:
            return resolved
        signed_in = [str(entry) for entry in self.session_store.list_accounts()]
        if not signed_in:
            raise MngrError(
                f"imbue_cloud provider '{self.name}' has no account configured and no "
                "imbue_cloud accounts are signed in. Run `mngr imbue_cloud auth signin "
                "--account <email>` first, then either bind the provider to it via "
                '`[providers.imbue_cloud_<slug>] account = "<email>"` or pass '
                "`-b account=<email>` on `mngr create`. Disable the imbue_cloud "
                "provider in your config if you don't intend to use it."
            )
        raise MngrError(
            f"imbue_cloud provider '{self.name}' has no active account but multiple "
            f"signed-in accounts exist ({signed_in}). Pick one with `mngr imbue_cloud "
            "auth use --account <email>`, pass `-b account=<email>` on `mngr create`, "
            "or pin the account in the provider config."
        )

    # ------------------------------------------------------------------
    # Lease bookkeeping
    # ------------------------------------------------------------------

    def generate_per_host_keypair(self, host_id: HostId) -> tuple[Path, str]:
        """Generate (or load) the SSH keypair used to authenticate to this host.

        Returns the private key path and the public key contents (so the caller
        can send the public key in the lease request).
        """
        return load_or_create_ssh_keypair(self._host_state_dir(host_id), "ssh_key")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _list_leased_hosts_cached(self) -> list[LeasedHostInfo]:
        """List leased hosts for this provider's resolved account.

        Raises (via ``_require_account``) when no account can be resolved
        -- enabled providers must be active. Disable the provider in
        config if you don't want this to participate in ``mngr list``.
        """
        if self._leased_hosts_cache is not None:
            return self._leased_hosts_cache
        account = self._require_account()
        token = self._get_access_token(account)
        try:
            self._leased_hosts_cache = self.client.list_hosts(token)
        except MngrError as exc:
            logger.warning("imbue_cloud[{}] list_hosts failed: {}", self.name, exc)
            self._leased_hosts_cache = []
        return self._leased_hosts_cache

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        leased = self._list_leased_hosts_cached()
        return [
            DiscoveredHost(
                host_id=HostId(entry.host_id),
                host_name=HostName(entry.host_name),
                provider_name=self.name,
                host_state=HostState.RUNNING,
            )
            for entry in leased
        ]

    # ------------------------------------------------------------------
    # Listing
    #
    # Discovery is outer-SSH-primary: for each lease we connect to the
    # VPS root (port 22) once and run ``build_outer_listing_collection_script``,
    # which dispatches to ``docker exec`` for a running container or to
    # ``docker cp`` (extracting host_dir to a tmp path) for a stopped one.
    # Either way we surface the container's actual state (RUNNING /
    # STOPPED / CRASHED / PAUSED / DESTROYED) plus the host's data.json
    # (friendly name, image, tags, agents). The cached raw output is then
    # consumed by ``get_host_and_agent_details`` without another SSH.
    #
    # Lease-only synthesis (with state=CRASHED and failure_reason carrying
    # the underlying error) is reserved for the last-resort case where
    # even the outer SSH is unreachable -- in normal operation we expect
    # outer SSH to be reachable for every leased VPS.
    # ------------------------------------------------------------------

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        leased = self._list_leased_hosts_cached()
        result: dict[DiscoveredHost, list[DiscoveredAgent]] = {}
        for entry in leased:
            host_id = HostId(entry.host_id)
            raw, outer_error, is_auth_failure = self._collect_listing_raw_via_outer(entry)
            if raw is None:
                # Outer SSH itself failed; fall back to a lease-only stub
                # so the host doesn't disappear from `mngr list`. The state
                # depends on whether the failure was an auth mismatch (the
                # host is reachable, our key is just wrong) or something
                # more terminal (network down, host destroyed).
                fallback_state = HostState.UNAUTHENTICATED if is_auth_failure else HostState.CRASHED
                host_ref = DiscoveredHost(
                    host_id=host_id,
                    host_name=HostName(entry.host_name),
                    provider_name=self.name,
                    host_state=fallback_state,
                )
                agent_refs = [
                    DiscoveredAgent(
                        agent_id=AgentId(entry.agent_id),
                        agent_name=AgentName(entry.agent_id),
                        host_id=host_id,
                        provider_name=self.name,
                    )
                ]
                # Stash the outer error so get_host_and_agent_details can
                # surface it in failure_reason without re-trying SSH.
                self._listing_raw_cache[host_id] = {
                    "outer_ssh_error": outer_error,
                    "outer_ssh_is_auth_failure": is_auth_failure,
                }
                result[host_ref] = agent_refs
                continue
            self._listing_raw_cache[host_id] = raw
            host_state = _derive_host_state_from_raw(raw)
            if host_state == HostState.DESTROYED and not include_destroyed:
                continue
            # ``entry.host_name`` is the canonical user-supplied name from the
            # connector. On-host certified data may lag (e.g. the bake's
            # initial value before a lease overwrites it), so the lease wins.
            host_ref = DiscoveredHost(
                host_id=host_id,
                host_name=HostName(entry.host_name),
                provider_name=self.name,
                host_state=host_state,
            )
            agent_refs: list[DiscoveredAgent] = []
            for agent_raw in raw.get("agents", []):
                data = agent_raw.get("data", {})
                agent_id_str = data.get("id")
                agent_name_str = data.get("name")
                if not agent_id_str or not agent_name_str:
                    continue
                agent_refs.append(
                    DiscoveredAgent(
                        agent_id=AgentId(agent_id_str),
                        agent_name=AgentName(agent_name_str),
                        host_id=host_id,
                        provider_name=self.name,
                    )
                )
            # If the outer-SSH discovery returned no agents (e.g. container
            # gone, or data.json is empty), still synthesize a single agent
            # from the lease so the host shows in the listing.
            if not agent_refs:
                agent_refs.append(
                    DiscoveredAgent(
                        agent_id=AgentId(entry.agent_id),
                        agent_name=AgentName(entry.agent_id),
                        host_id=host_id,
                        provider_name=self.name,
                    )
                )
            result[host_ref] = agent_refs
        return result

    def _collect_listing_raw_via_outer(
        self,
        lease: LeasedHostInfo,
    ) -> tuple[dict[str, Any] | None, str | None, bool]:
        """Run the outer listing script over root SSH on the leased VPS.

        Returns ``(raw, None, False)`` on success (where ``raw`` is the
        parsed output of ``build_outer_listing_collection_script``) or
        ``(None, error_message, is_auth_failure)`` when outer SSH can't be
        reached. ``is_auth_failure`` is True iff the failure was an
        authentication error (``HostAuthenticationError``) -- in that case
        the host is reachable but our key was rejected, which is the
        ``UNAUTHENTICATED`` state, not ``CRASHED``.
        """
        host_id = HostId(lease.host_id)
        host_dir = str(self.host_dir)
        try:
            # ``_ensure_outer_host_key_known`` is documented as best-effort
            # but performs disk I/O that could in principle raise; keep it
            # inside the guard so a single bad lease can never drop the
            # rest of the listing.
            self._ensure_outer_host_key_known(lease)
            with self.outer_host_for(host_id) as outer:
                assert outer is not None
                script = build_outer_listing_collection_script(str(host_id), host_dir, self.mngr_ctx.config.prefix)
                result = outer.execute_idempotent_command(script, timeout_seconds=60.0)
        except HostAuthenticationError as exc:
            logger.warning(
                "imbue_cloud[{}] outer SSH authentication failed for host {}: {}",
                self.name,
                host_id,
                exc,
            )
            return None, f"outer SSH authentication failed: {exc}", True
        except (HostConnectionError, HostNotFoundError, MngrError) as exc:
            logger.warning(
                "imbue_cloud[{}] outer SSH unreachable for host {}: {}",
                self.name,
                host_id,
                exc,
            )
            return None, f"outer SSH unreachable: {exc}", False
        if not result.success:
            logger.warning(
                "imbue_cloud[{}] outer listing script for host {} exited non-zero: {}",
                self.name,
                host_id,
                result.stderr.strip(),
            )
            return None, f"outer listing script failed: {result.stderr.strip() or 'non-zero exit'}", False
        return parse_listing_collection_output(result.stdout), None, False

    def get_host_and_agent_details(
        self,
        host_ref: DiscoveredHost,
        agent_refs: Sequence[DiscoveredAgent],
        field_generators: Mapping[str, Mapping[str, Callable[[AgentInterface, OnlineHostInterface], Any]]]
        | None = None,
        on_error: Callable[[DiscoveredAgent | DiscoveredHost, BaseException], None] | None = None,
    ) -> tuple[HostDetails, list[AgentDetails]]:
        """Build HostDetails + AgentDetails from the cached outer-listing output.

        ``discover_hosts_and_agents`` already did the single outer-SSH
        round-trip and cached the parsed data; this method just shapes it
        into the typed details structures. When the cached raw indicates
        outer SSH failed during discovery, fall back to lease-only details
        (state=CRASHED, ssh from lease, failure_reason carrying the
        original error).
        """
        host_id = host_ref.host_id
        lease = self._find_leased(host_id)
        if lease is None:
            return super().get_host_and_agent_details(host_ref, agent_refs, field_generators, on_error)
        raw = self._listing_raw_cache.get(host_id)
        if raw is None:
            # Discovery wasn't run for this host (rare; e.g. an explicit
            # detail call without going through `mngr list`); fall back.
            return self._build_offline_details_from_lease(host_ref, agent_refs, lease, "discovery did not run")
        outer_error = raw.get("outer_ssh_error")
        if outer_error is not None:
            return self._build_offline_details_from_lease(host_ref, agent_refs, lease, str(outer_error))
        host_details = self._build_host_details_from_raw(host_ref, lease, raw)
        agent_details_list: list[AgentDetails] = []
        ssh_activity = timestamp_to_datetime(raw.get("ssh_activity_mtime"))
        ps_output = raw.get("ps_output", "")
        for agent_raw in raw.get("agents", []):
            agent_details = self._build_agent_details_from_raw(
                agent_raw=agent_raw,
                host_details=host_details,
                ssh_activity=ssh_activity,
                ps_output=ps_output,
            )
            if agent_details is not None:
                agent_details_list.append(agent_details)
        # If the raw produced no agent details (stopped container with
        # empty agents dir, or a hung docker exec), synthesize one from
        # any agent_ref the caller passed in so the host still shows up
        # in the agent-driven listing table.
        if not agent_details_list and agent_refs:
            agent_details_list = [
                build_agent_details_from_offline_ref(agent_ref, host_details) for agent_ref in agent_refs
            ]
        return host_details, agent_details_list

    def _ensure_outer_host_key_known(self, lease: LeasedHostInfo) -> None:
        """Best-effort: scan the VPS root sshd's host key and add it to known_hosts.

        ``outer_host_for`` connects with strict host-key checking, but the
        lease step only added the inner container's host key (port 2222)
        to ``known_hosts``. Without this scan, the very first outer-SSH
        connection always fails. The scan and add are both idempotent and
        safe to run multiple times; on scan failure (e.g. the VPS itself
        is unreachable) or on local disk failure we just leave
        ``known_hosts`` alone and let the connection produce its natural
        error -- the caller's outer-SSH guard then maps that to the
        lease-only fallback.
        """
        scanned_key = _scan_ssh_host_key(lease.vps_address, 22)
        if scanned_key is None:
            return
        host_id = HostId(lease.host_id)
        try:
            known_hosts_path = self._host_known_hosts_path(host_id)
            known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
            if not known_hosts_path.exists():
                known_hosts_path.touch()
            add_host_to_known_hosts(known_hosts_path, lease.vps_address, 22, scanned_key)
        except OSError as exc:
            logger.warning(
                "imbue_cloud[{}] could not update known_hosts for host {} (vps {}): {}",
                self.name,
                host_id,
                lease.vps_address,
                exc,
            )

    def _build_lease_ssh_info(self, host_id: HostId, lease: LeasedHostInfo) -> SSHInfo:
        """Build the SSHInfo that points at a leased container's inner sshd.

        Shared by both the lease-only fallback and the cached-listing path so
        the two stay in lockstep if SSHInfo grows new fields or the rendered
        ``command`` string changes.
        """
        private_key_path, _ = self._host_keypair_paths(host_id)
        return SSHInfo(
            user=lease.ssh_user,
            host=lease.vps_address,
            port=lease.container_ssh_port,
            key_path=private_key_path,
            command=f"ssh -i {private_key_path} -p {lease.container_ssh_port} {lease.ssh_user}@{lease.vps_address}",
        )

    def _build_offline_details_from_lease(
        self,
        host_ref: DiscoveredHost,
        agent_refs: Sequence[DiscoveredAgent],
        lease: LeasedHostInfo,
        failure_message: str,
    ) -> tuple[HostDetails, list[AgentDetails]]:
        """Build HostDetails + AgentDetails from lease info when outer SSH is unreachable.

        Last-resort path: we have nothing but lease metadata. SSH info is
        populated so the user can see the unreachable address;
        ``failure_reason`` carries the underlying error. The state comes
        from ``host_ref.host_state`` (which discovery set to
        ``UNAUTHENTICATED`` for auth failures and ``CRASHED`` for other
        outer-SSH errors), with ``CRASHED`` as a safe default if it's
        unset.
        """
        ssh_info = self._build_lease_ssh_info(host_ref.host_id, lease)
        host_details = HostDetails(
            id=host_ref.host_id,
            name=str(host_ref.host_name),
            provider_name=host_ref.provider_name,
            state=host_ref.host_state or HostState.CRASHED,
            ssh=ssh_info,
            failure_reason=failure_message,
        )
        agent_details_list = [
            build_agent_details_from_offline_ref(agent_ref, host_details) for agent_ref in agent_refs
        ]
        return host_details, agent_details_list

    def _build_host_details_from_raw(
        self,
        host_ref: DiscoveredHost,
        lease: LeasedHostInfo,
        raw: dict[str, Any],
    ) -> HostDetails:
        """Build HostDetails from the cached outer-listing raw output.

        Works for both running containers (full data via ``docker exec``)
        and stopped containers (data.json + mtimes via ``docker cp``). The
        state is derived from ``container_state`` in the raw output (set
        by ``build_outer_listing_collection_script``).
        """
        ssh_info = self._build_lease_ssh_info(host_ref.host_id, lease)
        host_state = _derive_host_state_from_raw(raw)
        failure_reason = _derive_offline_note_from_raw(raw)
        boot_time = timestamp_to_datetime(raw.get("btime"))
        uptime_seconds = raw.get("uptime_seconds")
        lock_mtime = raw.get("lock_mtime")
        is_locked = lock_mtime is not None
        locked_time = datetime.fromtimestamp(lock_mtime, tz=timezone.utc) if lock_mtime is not None else None
        ssh_activity_mtime = raw.get("ssh_activity_mtime")
        ssh_activity = (
            datetime.fromtimestamp(ssh_activity_mtime, tz=timezone.utc) if ssh_activity_mtime is not None else None
        )
        # ``certified_data`` is the host-level data.json the pool host
        # baked at provision time. It carries image, idle settings, tags,
        # plugin state, etc. -- richer than what the lease object alone
        # tells us. For the friendly name we trust the lease (the
        # connector-side canonical, mutable per-lease) rather than the
        # baked-in certified data, which may still hold the bake-time
        # placeholder.
        certified = raw.get("certified_data") or {}
        host_name_str = lease.host_name
        image = certified.get("image", "")
        tags = dict(certified.get("user_tags", {}))
        plugin = dict(certified.get("plugin", {}))
        attributes = lease.attributes or {}
        cpus_attr = attributes.get("cpus")
        memory_attr = attributes.get("memory_gb")
        cpu_count = int(cpus_attr) if isinstance(cpus_attr, (int, float)) else 1
        memory_gb = float(memory_attr) if isinstance(memory_attr, (int, float)) else 1.0
        resource = HostResources(cpu=CpuResources(count=cpu_count), memory_gb=memory_gb, disk_gb=None, gpu=None)
        return HostDetails(
            id=host_ref.host_id,
            name=HostName(host_name_str),
            provider_name=host_ref.provider_name,
            state=host_state,
            image=image,
            tags=tags,
            boot_time=boot_time,
            uptime_seconds=uptime_seconds,
            resource=resource,
            ssh=ssh_info,
            snapshots=[],
            is_locked=is_locked,
            locked_time=locked_time,
            plugin=plugin,
            ssh_activity_time=ssh_activity,
            failure_reason=failure_reason,
        )

    def _build_agent_details_from_raw(
        self,
        agent_raw: dict[str, Any],
        host_details: HostDetails,
        ssh_activity: datetime | None,
        ps_output: str,
    ) -> AgentDetails | None:
        """Construct one ``AgentDetails`` from the parsed listing output.

        Mirrors ``mngr_vps_docker``'s implementation -- the fields are
        identical because both providers consume the same shared listing
        script. We pull idle/activity-source metadata off the per-agent
        ``data.json`` that the script captured rather than off a
        provider-side cache, so this works for any pool host regardless
        of how it was originally baked.
        """
        agent_data = agent_raw.get("data", {})
        agent_id_str = agent_data.get("id")
        agent_name_str = agent_data.get("name")
        if not agent_id_str or not agent_name_str:
            logger.warning("imbue_cloud[{}] skipping agent missing id/name in listing data", self.name)
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
        except (ValueError, TypeError) as exc:
            logger.warning("imbue_cloud[{}] failed to parse create_time for {}: {}", self.name, agent_id_str, exc)
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
        idle_timeout_raw = agent_data.get("idle_timeout_seconds", 800)
        idle_mode_value = agent_data.get("idle_mode", "DISABLED")
        activity_sources = tuple(agent_data.get("activity_sources", ()))
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
            idle_mode=idle_mode_value,
            idle_timeout_seconds=int(idle_timeout_raw) if idle_timeout_raw is not None else 800,
            activity_sources=tuple(str(s) for s in activity_sources),
            labels=agent_data.get("labels", {}),
            host=host_details,
            plugin={},
        )

    def _build_host_object(self, lease: LeasedHostInfo) -> ImbueCloudHost:
        host_id = HostId(lease.host_id)
        agent_id = AgentId(lease.agent_id)
        ssh_user = lease.ssh_user
        vps_address = lease.vps_address
        container_ssh_port = lease.container_ssh_port
        host_db_id = str(lease.host_db_id)

        private_key_path, _ = self._host_keypair_paths(host_id)
        if not private_key_path.exists():
            # No local keypair -- this happens when discovering hosts that were
            # leased on another machine. Generate a placeholder so SSH fails
            # explicitly later rather than crashing in pyinfra setup.
            self.generate_per_host_keypair(host_id)
            private_key_path, _ = self._host_keypair_paths(host_id)

        known_hosts_path = self._host_known_hosts_path(host_id)
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        if not known_hosts_path.exists():
            known_hosts_path.touch()

        pyinfra_host = create_pyinfra_host(
            hostname=vps_address,
            port=container_ssh_port,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
        )
        connector = PyinfraConnector(pyinfra_host)
        host = ImbueCloudHost(
            id=host_id,
            host_name=HostName(lease.host_name),
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
            pre_baked_agent_id=agent_id,
            lease_db_id=host_db_id,
        )
        self._evict_cached_host(host_id, replacement=host)
        return host

    def get_host(
        self,
        host: HostId | HostName,
    ) -> Host:
        leased = self._list_leased_hosts_cached()
        for entry in leased:
            if isinstance(host, HostId) and entry.host_id == str(host):
                return self._build_host_object(entry)
            if isinstance(host, HostName) and entry.host_name == str(host):
                return self._build_host_object(entry)
        raise HostNotFoundError(host)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Build an OfflineHost from the connector's lease metadata.

        The lease has no certified host data (that lives on the host itself,
        readable only via SSH). For the offline path used by ``mngr list``
        when SSH fails, we synthesize a minimal ``CertifiedHostData`` from
        the lease so the listing layer can still produce a row.
        """
        lease = self._find_leased(host_id)
        if lease is None:
            raise HostNotFoundError(host_id)
        now = datetime.now(timezone.utc)
        certified_host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=lease.host_name,
            created_at=now,
            updated_at=now,
        )
        return OfflineHost(
            id=host_id,
            certified_host_data=certified_host_data,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )

    def get_host_resources(self, host: HostInterface) -> HostResources:
        leased = self._list_leased_hosts_cached()
        for entry in leased:
            if entry.host_id == str(host.id):
                attrs = entry.attributes
                cpus = int(attrs.get("cpus", 1)) if isinstance(attrs.get("cpus"), int) else 1
                memory = (
                    float(attrs.get("memory_gb", 1.0)) if isinstance(attrs.get("memory_gb"), (int, float)) else 1.0
                )
                return HostResources(cpu=CpuResources(count=cpus), memory_gb=memory, disk_gb=None, gpu=None)
        return HostResources(cpu=CpuResources(count=1), memory_gb=1.0, disk_gb=None, gpu=None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

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
        """Lease a pool host whose attributes match ``build_args`` and return it.

        Two address forms work:
          - ``mngr create my-agent@.imbue_cloud_alice --new-host -b cpus=4 -b
            version=...`` -- the per-account provider instance carries the
            account in ``config.account`` and the build args are pure lease
            attributes.
          - ``mngr create my-agent@.imbue_cloud --new-host -b
            account=alice@imbue.com -b cpus=4 -b version=...`` -- the
            default instance has no account; the caller passes one through
            build args.

        ``account`` is extracted from ``build_args``; remaining keys are
        parsed into ``LeaseAttributes`` and sent to the connector, which
        finds an available pool host whose ``attributes`` JSONB row
        matches. The returned ``ImbueCloudHost`` carries the pre-baked
        agent id so the rest of mngr's create pipeline (agent state, env
        injection, agent start) can adopt the existing agent under the
        caller's chosen name.
        """
        if snapshot is not None:
            raise SnapshotsNotSupportedError(self.name)
        if image is not None or start_args:
            raise MngrError(
                "imbue_cloud provider does not accept --image or --start-arg; "
                "use --build-arg KEY=VALUE flags to constrain the lease attributes."
            )
        try:
            attributes, account_override = LeaseAttributes.from_build_args(build_args)
        except ValueError as exc:
            raise MngrError(f"Invalid build_args for imbue_cloud lease: {exc}") from exc

        account = self._require_account(account_override)
        token = self._get_access_token(account)
        # The lease request needs a host_id placeholder so we can stash the
        # per-host keypair under its canonical path before we know the
        # pool-baked id. We generate a temp dir, then move the keys into the
        # canonical hosts/<lease.host_id>/ once the lease comes back.
        provider_dir = self._provider_data_dir()
        leases_dir = provider_dir / "leases"
        leases_dir.mkdir(parents=True, exist_ok=True)
        tmp_key_dir = leases_dir / f"pending-{int(time.time() * 1000)}"
        tmp_key_dir.mkdir(parents=True, exist_ok=True)
        tmp_private_key, tmp_public_key = save_ssh_keypair(tmp_key_dir, "ssh_key")
        public_key_text = tmp_public_key.read_text().strip()

        # ``name`` is the user-supplied HostName; send it to the connector so
        # the leased pool row carries the same friendly name minds renders to
        # the user. The connector validates it server-side too.
        lease_result = self.client.lease_host(token, attributes, public_key_text, str(name))
        self.reset_caches()

        host_id = HostId(lease_result.host_id)
        host_state_dir = self._host_state_dir(host_id)
        host_state_dir.mkdir(parents=True, exist_ok=True)
        final_private_key = host_state_dir / "ssh_key"
        final_public_key = host_state_dir / "ssh_key.pub"
        tmp_private_key.replace(final_private_key)
        tmp_public_key.replace(final_public_key)
        final_private_key.chmod(0o600)
        # Best-effort cleanup of the pending dir; fails harmlessly if a
        # concurrent lease left peers behind.
        try:
            tmp_key_dir.rmdir()
        except OSError:
            pass

        # Persist a small lease metadata file so subsequent commands (and
        # ``hosts release``) can find host_db_id without going to the connector.
        lease_meta_path = host_state_dir / "lease.json"
        lease_meta_path.write_text(json.dumps(lease_result.model_dump(), indent=2, default=str))

        # Wait for the leased container's sshd to be ready before we hand the
        # host back to mngr's create pipeline (which will SSH in immediately
        # to write the agent env file and start tmux).
        wait_for_sshd(lease_result.vps_address, lease_result.container_ssh_port, _SSH_WAIT_TIMEOUT_SECONDS)

        # Try to scan the container's host key so strict host-key checking
        # succeeds. This is best-effort: if the scan fails we leave the
        # known_hosts file empty and rely on mngr's auto-add policy.
        known_hosts_path = self._host_known_hosts_path(host_id)
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        if not known_hosts_path.exists():
            known_hosts_path.touch()
        scanned_key = _scan_ssh_host_key(lease_result.vps_address, lease_result.container_ssh_port)
        if scanned_key is not None:
            add_host_to_known_hosts(
                known_hosts_path,
                lease_result.vps_address,
                lease_result.container_ssh_port,
                scanned_key,
            )

        leased_info = LeasedHostInfo(
            host_db_id=lease_result.host_db_id,
            vps_address=lease_result.vps_address,
            ssh_port=lease_result.ssh_port,
            ssh_user=lease_result.ssh_user,
            container_ssh_port=lease_result.container_ssh_port,
            agent_id=lease_result.agent_id,
            host_id=lease_result.host_id,
            host_name=lease_result.host_name,
            attributes=lease_result.attributes,
            leased_at="",
        )
        return self._build_host_object(leased_info)

    def _resolve_container_id_on_outer(self, outer: OuterHostInterface, host_id: HostId) -> str | None:
        """Look up the docker container id for the given inner host on its outer VPS.

        Returns None when no container with that label exists. Containers are
        identified by ``com.imbue.mngr.host-id=<host_id>`` (the canonical
        ``LABEL_HOST_ID`` from ``mngr_vps_docker``).
        """
        result = outer.execute_idempotent_command(
            f"docker ps -aq --filter label=com.imbue.mngr.host-id={shlex.quote(str(host_id))} | head -1"
        )
        if not result.success:
            raise MngrError(f"failed to look up container for host {host_id} on outer: {result.stderr.strip()}")
        container_id = result.stdout.strip()
        return container_id or None

    def _run_outer_docker_command(
        self,
        outer: OuterHostInterface,
        docker_args: str,
        *,
        host_id: HostId,
        label: str,
    ) -> str:
        """Run ``docker <docker_args>`` on the outer host; raise on non-zero exit."""
        result = outer.execute_idempotent_command(f"docker {docker_args}")
        if not result.success:
            raise MngrError(
                f"VPS root SSH command {label!r} failed for host {host_id}: "
                f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
            )
        return result.stdout.strip()

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop the docker container on the leased VPS via the outer host.

        The lease step authorized this provider's per-host SSH key on the VPS
        root account at port 22, so the outer host can ``docker stop`` the
        container labeled with this host_id. The lease and on-disk volume
        are preserved; ``start_host`` brings the container back later.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        # outer_host_for raises HostNotFoundError if the lease/key isn't found,
        # so the yielded outer is always non-None for this provider.
        with self.outer_host_for(host_id) as outer:
            assert outer is not None
            container_id = self._resolve_container_id_on_outer(outer, host_id)
            if container_id is None:
                logger.debug("stop_host: no container for host {}; nothing to do", host_id)
                return
            self._run_outer_docker_command(
                outer, f"stop {shlex.quote(container_id)}", host_id=host_id, label="docker-stop"
            )
            logger.debug("Stopped container {} for host {}", container_id, host_id)

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Start the previously-stopped docker container via the outer host and return the Host."""
        host_id = host.id if isinstance(host, HostInterface) else host
        leased = self._find_leased(host_id)
        if leased is None:
            raise HostNotFoundError(host_id)
        if snapshot_id is not None:
            raise SnapshotsNotSupportedError(self.name)
        with self.outer_host_for(host_id) as outer:
            assert outer is not None
            container_id = self._resolve_container_id_on_outer(outer, host_id)
            if container_id is None:
                raise MngrError(
                    f"start_host: no docker container with label com.imbue.mngr.host-id={host_id} on {leased.vps_address}"
                )
            self._run_outer_docker_command(
                outer, f"start {shlex.quote(container_id)}", host_id=host_id, label="docker-start"
            )
            logger.debug("Started container {} for host {}", container_id, host_id)
        return self._build_host_object(leased)

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Stop the leased container (does NOT release the lease).

        Matches the architect-spec definition of destroy: the docker container
        is stopped on the VPS but the lease, on-disk volume, and any in-progress
        agent work persist. Use ``delete_host`` (or ``mngr imbue_cloud hosts
        release``) to release the lease back to the pool.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        leased = self._find_leased(host_id)
        if leased is None:
            logger.warning("destroy_host: no lease record for host {}; nothing to do", host_id)
            return
        try:
            with self.outer_host_for(host_id) as outer:
                assert outer is not None
                container_id = self._resolve_container_id_on_outer(outer, host_id)
                if container_id is None:
                    logger.debug("destroy_host: no container for host {}; nothing to do", host_id)
                else:
                    self._run_outer_docker_command(
                        outer, f"stop {shlex.quote(container_id)}", host_id=host_id, label="docker-stop"
                    )
                    logger.debug("Stopped container {} for host {}", container_id, host_id)
        except HostNotFoundError:
            logger.warning(
                "destroy_host: SSH key for host {} is missing; cannot stop container remotely. "
                "Run `mngr imbue_cloud hosts release` to release the lease instead.",
                host_id,
            )
            return
        self.reset_caches()

    def delete_host(self, host: HostInterface) -> None:
        """Release the lease back to the pool and drop local state.

        Called by mngr's GC after the destroyed-host grace period (or directly
        when an operator wants the lease freed immediately). The lease return
        is the authoritative step here; container removal is best-effort
        because the connector will reuse the VPS for a new lease anyway.
        """
        host_id = host.id
        host_db_id = self._resolve_host_db_id(host, host_id)
        leased = self._find_leased(host_id)
        if leased is not None:
            try:
                with self.outer_host_for(host_id) as outer:
                    assert outer is not None
                    container_id = self._resolve_container_id_on_outer(outer, host_id)
                    if container_id is not None:
                        self._run_outer_docker_command(
                            outer,
                            f"rm -f -v {shlex.quote(container_id)}",
                            host_id=host_id,
                            label="docker-rm",
                        )
                        logger.debug("Removed container {} for host {}", container_id, host_id)
            except (HostNotFoundError, MngrError) as exc:
                logger.warning("delete_host: failed to remove container for host {}: {}", host_id, exc)
        if host_db_id is not None:
            account = self._require_account()
            token = self._get_access_token(account)
            self.client.release_host(token, host_db_id)
        self._cleanup_local_host_state(host_id)

    def _resolve_host_db_id(
        self,
        host: HostInterface | HostId,
        host_id: HostId,
    ) -> str | None:
        """Find the lease's database id for a host, falling back to a discovery scan."""
        if isinstance(host, ImbueCloudHost) and host.lease_db_id is not None:
            return host.lease_db_id
        leased = self._find_leased(host_id)
        return str(leased.host_db_id) if leased is not None else None

    def _cleanup_local_host_state(self, host_id: HostId) -> None:
        host_state_dir = self._host_state_dir(host_id)
        if host_state_dir.exists():
            try:
                _rm_tree(host_state_dir)
            except OSError as exc:
                logger.warning("Failed to remove host state dir {}: {}", host_state_dir, exc)
        self.reset_caches()
        self._evict_cached_host(host_id)

    def _find_leased(self, host_id: HostId) -> LeasedHostInfo | None:
        for entry in self._list_leased_hosts_cached():
            if entry.host_id == str(host_id):
                return entry
        return None

    def on_connection_error(self, host_id: HostId) -> None:
        """A connection error doesn't change connector-side lease state; just clear our cache."""
        self.reset_caches()

    def outer_host_id_for(self, host_id: HostId) -> str | None:
        """Stable id for the outer (leased VPS) of host_id, keyed by VPS IP."""
        leased = self._find_leased(host_id)
        if leased is None:
            raise HostNotFoundError(host_id)
        return f"outer:{self.name}:{leased.vps_address}"

    @contextmanager
    def outer_host_for(self, host_id: HostId) -> Iterator[OuterHostInterface | None]:
        """Open the outer host (the leased VPS itself, root@vps_address:22).

        Uses the per-host SSH key already on disk (the lease step authorized
        this key on both the container's sshd and the VPS root account).
        """
        leased = self._find_leased(host_id)
        if leased is None:
            raise HostNotFoundError(host_id)
        private_key_path, _ = self._host_keypair_paths(host_id)
        if not private_key_path.exists():
            raise HostNotFoundError(host_id)

        known_hosts_path = self._host_known_hosts_path(host_id)
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        if not known_hosts_path.exists():
            known_hosts_path.touch()

        pyinfra_host = create_pyinfra_host(
            hostname=leased.vps_address,
            port=22,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            ssh_user="root",
        )
        outer = OuterHost(
            id=host_id,
            connector=PyinfraConnector(pyinfra_host),
            mngr_ctx=self.mngr_ctx,
        )
        try:
            yield outer
        finally:
            outer.disconnect()

    # ------------------------------------------------------------------
    # Snapshots / volumes / tags / rename: not supported
    # ------------------------------------------------------------------

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

    def list_volumes(self) -> list[VolumeInfo]:
        return []

    def delete_volume(self, volume_id: VolumeId) -> None:
        raise NotImplementedError("imbue_cloud does not support volumes")

    def get_host_tags(
        self,
        host: HostInterface | HostId,
    ) -> dict[str, str]:
        return {}

    def set_host_tags(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        raise NotImplementedError("imbue_cloud does not support mutable host tags")

    def add_tags_to_host(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        raise NotImplementedError("imbue_cloud does not support mutable host tags")

    def remove_tags_from_host(
        self,
        host: HostInterface | HostId,
        keys: Sequence[str],
    ) -> None:
        raise NotImplementedError("imbue_cloud does not support mutable host tags")

    def rename_host(
        self,
        host: HostInterface | HostId,
        name: HostName,
    ) -> Host:
        raise NotImplementedError("imbue_cloud does not support renaming hosts (the host_id is fixed by the lease)")

    # ------------------------------------------------------------------
    # pyinfra connector lookup
    # ------------------------------------------------------------------

    def get_connector(
        self,
        host: HostInterface | HostId,
    ) -> Any:
        host_id = host.id if isinstance(host, HostInterface) else host
        host_obj = self.get_host(host_id)
        return host_obj.connector.host


def _rm_tree(path: Path) -> None:
    """Recursively delete a path, raising the first OSError encountered."""
    if path.is_file() or path.is_symlink():
        path.unlink()
        return
    for child in path.iterdir():
        _rm_tree(child)
    path.rmdir()
