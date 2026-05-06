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
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
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
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
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
from imbue.mngr.providers.listing_utils import build_listing_collection_script
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_imbue_cloud import vps_admin
from imbue.mngr_imbue_cloud.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_provider_data_dir
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.host import ImbueCloudHost
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore

_SSH_WAIT_TIMEOUT_SECONDS: Final[float] = 120.0


def _scan_container_host_key(vps_ip: str, container_ssh_port: int) -> str | None:
    """Best-effort: pull the leased container's sshd public key for known_hosts.

    Returns ``"<key_type> <base64>"`` on success, or ``None`` on any failure
    (timeout, connection refused, protocol error). Callers add this to
    ``known_hosts`` so subsequent SSH connections succeed under
    ``StrictHostKeyChecking``.
    """
    transport = paramiko.Transport((vps_ip, container_ssh_port))
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
    # Cache of the parsed listing-script output keyed by host_id, populated by
    # ``discover_hosts_and_agents`` and consumed by
    # ``get_host_and_agent_details`` so a single SSH round-trip (per host)
    # serves both phases of ``mngr list``.
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
                host_name=HostName(entry.host_id),
                provider_name=self.name,
                host_state=HostState.RUNNING,
            )
            for entry in leased
        ]

    # ------------------------------------------------------------------
    # Optimized listing
    #
    # The default ``discover_hosts_and_agents`` and
    # ``get_host_and_agent_details`` implementations on
    # ``BaseProviderInstance`` reach out to the leased container many
    # times per host (``ls``, ``stat``, ``ps``, ``tmux``, ...), so on a
    # high-RTT remote VPS ``mngr list`` ends up doing ~15 sequential
    # SSH round-trips per host. The override below collects everything
    # we need with one ``build_listing_collection_script`` execution per
    # host and caches the parsed output for the second phase to reuse.
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
            host_ref = DiscoveredHost(
                host_id=host_id,
                host_name=HostName(entry.host_id),
                provider_name=self.name,
                host_state=HostState.RUNNING,
            )
            try:
                raw = self._collect_listing_raw(entry)
            except (HostConnectionError, MngrError) as exc:
                logger.warning("imbue_cloud[{}] listing collection for {} failed: {}", self.name, host_id, exc)
                self.on_connection_error(host_id)
                result[host_ref] = []
                continue
            self._listing_raw_cache[host_id] = raw
            agent_refs: list[DiscoveredAgent] = []
            for agent_raw in raw.get("agents", []):
                data = agent_raw.get("data", {})
                agent_id_str = data.get("id")
                agent_name_str = data.get("name")
                if not agent_id_str or not agent_name_str:
                    logger.debug("imbue_cloud[{}] skipping agent missing id/name: {}", self.name, data)
                    continue
                agent_refs.append(
                    DiscoveredAgent(
                        agent_id=AgentId(agent_id_str),
                        agent_name=AgentName(agent_name_str),
                        host_id=host_id,
                        provider_name=self.name,
                    )
                )
            result[host_ref] = agent_refs
        return result

    def _collect_listing_raw(self, lease: LeasedHostInfo) -> dict[str, Any]:
        """Run ``build_listing_collection_script`` once on the leased host.

        Builds the host (which sets up the pyinfra connector) and runs the
        shared listing script. Returns the parsed dict; raises
        ``HostConnectionError`` / ``MngrError`` on SSH or remote failures
        so the caller can decide to fall back.
        """
        host = self._build_host_object(lease)
        script = build_listing_collection_script(str(host.host_dir), self.mngr_ctx.config.prefix)
        result = host.execute_idempotent_command(script, timeout_seconds=30.0)
        if not result.success:
            raise MngrError(
                f"imbue_cloud listing script on host {lease.host_id} exited non-zero: {result.stderr.strip()}"
            )
        return parse_listing_collection_output(result.stdout)

    def get_host_and_agent_details(
        self,
        host_ref: DiscoveredHost,
        agent_refs: Sequence[DiscoveredAgent],
        field_generators: Mapping[str, Mapping[str, Callable[[AgentInterface, OnlineHostInterface], Any]]]
        | None = None,
        on_error: Callable[[DiscoveredAgent | DiscoveredHost, BaseException], None] | None = None,
    ) -> tuple[HostDetails, list[AgentDetails]]:
        """Build HostDetails + AgentDetails from the cached listing output.

        Falls back to the framework's default (per-field SSH) when the
        cache is cold or the cached entry can't be matched to a current
        lease (rare; happens if the lease was released between phases).
        """
        host_id = host_ref.host_id
        raw = self._listing_raw_cache.get(host_id)
        lease = self._find_leased(host_id)
        if raw is None or lease is None:
            return super().get_host_and_agent_details(host_ref, agent_refs, field_generators, on_error)
        try:
            host = self.get_host(host_id)
        except HostNotFoundError:
            return super().get_host_and_agent_details(host_ref, agent_refs, field_generators, on_error)
        host_details = self._build_host_details_from_raw(host, host_ref, lease, raw)
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
        return host_details, agent_details_list

    def _build_host_details_from_raw(
        self,
        host: Host,
        host_ref: DiscoveredHost,
        lease: LeasedHostInfo,
        raw: dict[str, Any],
    ) -> HostDetails:
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
        lock_mtime = raw.get("lock_mtime")
        is_locked = lock_mtime is not None
        locked_time = datetime.fromtimestamp(lock_mtime, tz=timezone.utc) if lock_mtime is not None else None
        ssh_activity_mtime = raw.get("ssh_activity_mtime")
        ssh_activity = (
            datetime.fromtimestamp(ssh_activity_mtime, tz=timezone.utc) if ssh_activity_mtime is not None else None
        )
        # ``certified_data`` is the host-level data.json the pool host
        # baked at provision time. It carries name, image, idle settings,
        # tags, plugin state, etc. -- richer than what the lease object
        # alone tells us. Fall back to lease-level defaults when the
        # remote read produced an empty dict.
        certified = raw.get("certified_data") or {}
        host_name_str = certified.get("host_name") or lease.host_id
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
            id=host.id,
            name=HostName(host_name_str),
            provider_name=host_ref.provider_name,
            state=HostState.RUNNING,
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
            failure_reason=None,
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
        vps_ip = lease.vps_ip
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
            hostname=vps_ip,
            port=container_ssh_port,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
        )
        connector = PyinfraConnector(pyinfra_host)
        host = ImbueCloudHost(
            id=host_id,
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
            if isinstance(host, HostName) and entry.host_id == str(host):
                return self._build_host_object(entry)
        raise HostNotFoundError(host)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        raise NotImplementedError("imbue_cloud does not yet support offline hosts; lease state is server-side")

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

        lease_result = self.client.lease_host(token, attributes, public_key_text)
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
        wait_for_sshd(lease_result.vps_ip, lease_result.container_ssh_port, _SSH_WAIT_TIMEOUT_SECONDS)

        # Try to scan the container's host key so strict host-key checking
        # succeeds. This is best-effort: if the scan fails we leave the
        # known_hosts file empty and rely on mngr's auto-add policy.
        known_hosts_path = self._host_known_hosts_path(host_id)
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        if not known_hosts_path.exists():
            known_hosts_path.touch()
        scanned_key = _scan_container_host_key(lease_result.vps_ip, lease_result.container_ssh_port)
        if scanned_key is not None:
            add_host_to_known_hosts(
                known_hosts_path,
                lease_result.vps_ip,
                lease_result.container_ssh_port,
                scanned_key,
            )

        leased_info = LeasedHostInfo(
            host_db_id=lease_result.host_db_id,
            vps_ip=lease_result.vps_ip,
            ssh_port=lease_result.ssh_port,
            ssh_user=lease_result.ssh_user,
            container_ssh_port=lease_result.container_ssh_port,
            agent_id=lease_result.agent_id,
            host_id=lease_result.host_id,
            attributes=lease_result.attributes,
            leased_at="",
        )
        return self._build_host_object(leased_info)

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop the docker container on the leased VPS via root SSH.

        The lease step authorized this provider's per-host SSH key on the VPS
        root account at port 22, so we connect there and ``docker stop`` the
        container labeled with this host_id. The lease and on-disk volume
        are preserved; ``start_host`` brings the container back later.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        leased = self._find_leased(host_id)
        if leased is None:
            raise HostNotFoundError(host_id)
        private_key_path, _ = self._host_keypair_paths(host_id)
        if not private_key_path.exists():
            raise ImbueCloudConnectorError(
                f"stop_host: per-host SSH key for {host_id} is missing at {private_key_path}; "
                f"this lease was created on a different machine."
            )
        vps_admin.stop_container(leased.vps_ip, str(host_id), private_key_path)

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Start the previously-stopped docker container via root SSH and return the Host."""
        host_id = host.id if isinstance(host, HostInterface) else host
        leased = self._find_leased(host_id)
        if leased is None:
            raise HostNotFoundError(host_id)
        if snapshot_id is not None:
            raise SnapshotsNotSupportedError(self.name)
        private_key_path, _ = self._host_keypair_paths(host_id)
        if not private_key_path.exists():
            raise ImbueCloudConnectorError(
                f"start_host: per-host SSH key for {host_id} is missing at {private_key_path}."
            )
        vps_admin.start_container(leased.vps_ip, str(host_id), private_key_path)
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
        private_key_path, _ = self._host_keypair_paths(host_id)
        if not private_key_path.exists():
            logger.warning(
                "destroy_host: SSH key for host {} missing at {}; cannot stop container remotely. "
                "Run `mngr imbue_cloud hosts release` to release the lease instead.",
                host_id,
                private_key_path,
            )
            return
        vps_admin.stop_container(leased.vps_ip, str(host_id), private_key_path)
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
            private_key_path, _ = self._host_keypair_paths(host_id)
            if private_key_path.exists():
                try:
                    vps_admin.remove_container(leased.vps_ip, str(host_id), private_key_path)
                except ImbueCloudConnectorError as exc:
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
