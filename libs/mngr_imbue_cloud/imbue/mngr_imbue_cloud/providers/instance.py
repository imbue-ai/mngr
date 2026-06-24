"""ImbueCloudProvider: discover, destroy, delete leased pool hosts.

Lease creation is intentionally NOT done as part of `mngr create --provider
imbue_cloud_<account>`. Users go through `mngr imbue_cloud claim` (which is
the analogue of today's minds LEASED flow consolidated into the plugin).
That command produces a lease, registers the host with the connector, and
runs the rename + label + env-injection sequence in 2 SSH round trips.

This provider's responsibilities are then:
- `discover_hosts` -- list this account's leased hosts via the connector.
- `get_host` -- build a Host pointing at the leased VPS:container_ssh_port.
- `destroy_host` -- wipe the user's data on the leased VPS (container, named
  volumes, per-host btrfs subvolume under ``/mngr-btrfs/``, ``docker system
  prune``, ``/root`` and ``/tmp`` content) and release the lease back to the
  pool. The privacy-first ordering means the agent's data is gone before the
  connector flips the row to ``released``; ``cleanup_released_hosts.py``'s
  later VPS-destroy becomes belt-and-suspenders.
- `delete_host` -- called by mngr's GC after the destroyed-host grace
  period. Same flow as ``destroy_host``; treated as a no-op when the lease
  has already been released.
- `start_host` -- start the docker container on the VPS (no-op for an
  already-destroyed host; ``destroy_host`` is terminal for imbue_cloud).
- `stop_host` -- stop the docker container on the VPS without releasing
  the lease (use ``mngr stop`` when you intend to resume the workspace
  later on the same VPS).
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
from typing import assert_never

import httpx
import paramiko
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import compute_idle_seconds
from imbue.mngr.hosts.common import determine_lifecycle_state
from imbue.mngr.hosts.common import resolve_expected_process_name
from imbue.mngr.hosts.common import timestamp_to_datetime
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
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
from imbue.mngr.providers.ssh_utils import format_as_known_hosts_address
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_provider_data_dir
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.data_types import LeaseResult
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.data_types import parse_imbue_cloud_build_args
from imbue.mngr_imbue_cloud.errors import FastPathUnavailableError
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.errors import ImbueCloudLeaseUnavailableError
from imbue.mngr_imbue_cloud.errors import RepoIdentityError
from imbue.mngr_imbue_cloud.hosts.host import ImbueCloudHost
from imbue.mngr_imbue_cloud.primitives import FAST_PATH_ADOPTABLE_START_ARGS
from imbue.mngr_imbue_cloud.primitives import FastMode
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.providers.listing import derive_host_state_from_raw
from imbue.mngr_imbue_cloud.providers.listing import derive_offline_note_from_raw
from imbue.mngr_imbue_cloud.providers.rebuild import build_delegated_vps_provider
from imbue.mngr_imbue_cloud.providers.rebuild import build_slice_rebuild_provider
from imbue.mngr_imbue_cloud.providers.wipe import build_pool_host_wipe_script
from imbue.mngr_imbue_cloud.repo_identity import canonicalize_repo_source
from imbue.mngr_vps.container_setup import docker_inspect_running
from imbue.mngr_vps.container_setup import start_container_sshd
from imbue.mngr_vps.host_setup import apply_host_setup_on_outer
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.primitives import VpsInstanceId

_SSH_WAIT_TIMEOUT_SECONDS: Final[float] = 120.0


def _resolve_fast_path_attributes(attributes: LeaseAttributes) -> LeaseAttributes:
    """Canonicalize repo_url and require repo_url + repo_branch_or_tag for the fast path.

    The fast path adopts a pre-baked host only when the request's repo and branch
    genuinely match what was baked (decision 6 of the fast-path-matching spec): it
    must never match on a subset. Any failure to establish a sound identity --
    missing repo_url/branch, or a local path with no resolvable ``origin`` --
    raises ``FastPathUnavailableError`` so the caller falls back to the slow path
    (which rebuilds the user's exact spec) rather than adopting the wrong host.
    """
    if not attributes.repo_url or not attributes.repo_branch_or_tag:
        raise FastPathUnavailableError(
            "fast_mode=require needs both a repo_url and a repo_branch_or_tag to match a pool host; "
            f"got repo_url={attributes.repo_url!r}, repo_branch_or_tag={attributes.repo_branch_or_tag!r}"
        )
    try:
        canonical_repo_url = canonicalize_repo_source(attributes.repo_url)
    except RepoIdentityError as exc:
        raise FastPathUnavailableError(
            f"cannot establish a canonical repo identity for fast_mode=require: {exc}"
        ) from exc
    return attributes.model_copy_update(to_update(attributes.field_ref().repo_url, canonical_repo_url))


def _rewrite_container_host_name(
    *,
    vps_address: str,
    container_ssh_port: int,
    private_key_path: Path,
    known_hosts_path: Path,
    new_host_name: str,
    data_json_path: str = "/mngr/data.json",
    connect_timeout_seconds: float = 30.0,
) -> None:
    """Rewrite ``data.json``'s ``host_name`` field on the leased container.

    The pool host's ``/mngr/data.json`` was written at bake time with the
    bake's per-bake unique placeholder host name (``pool-<hex>-host``).
    The FCT bootstrap reads that file to decide what to name the initial
    chat agent (see ``forever-claude-template/libs/bootstrap/src/bootstrap/
    manager.py:_read_host_name``). Without this rewrite, every lease would
    end up with a chat agent named after the bake's placeholder instead
    of the user's chosen workspace name.

    Implementation: SFTP download -> mutate the parsed dict -> SFTP upload.
    Avoids the shell-quoting hazards of an inline ``python3 -c`` over
    ``exec_command`` (the host name flows through user input ultimately,
    so single/double-quote escaping has to be 100% airtight).

    Raises ``MngrError`` on any SSH, SFTP, or JSON failure -- a wrong
    ``host_name`` is exactly the bug this exists to prevent, so a silent
    fallback would re-introduce it.
    """
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(str(known_hosts_path))
    except OSError as exc:
        raise MngrError(f"failed to load known_hosts {known_hosts_path} for host_name rewrite: {exc}") from exc
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=vps_address,
            port=container_ssh_port,
            username="root",
            key_filename=str(private_key_path),
            allow_agent=False,
            look_for_keys=False,
            timeout=connect_timeout_seconds,
        )
    except (paramiko.SSHException, OSError) as exc:
        raise MngrError(
            f"SSH connect for host_name rewrite on {vps_address}:{container_ssh_port} failed: {exc}"
        ) from exc
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(data_json_path, "r") as remote:
                raw = remote.read()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MngrError(f"{data_json_path} on leased host {vps_address} is not valid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise MngrError(f"{data_json_path} on leased host {vps_address} did not parse to an object")
            data["host_name"] = new_host_name
            payload = json.dumps(data, indent=2).encode()
            with sftp.open(data_json_path, "w") as remote:
                remote.write(payload)
        finally:
            try:
                sftp.close()
            except (paramiko.SSHException, OSError):
                pass
    finally:
        try:
            client.close()
        except (paramiko.SSHException, OSError):
            pass


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
        # Do NOT swallow a discovery failure to an empty list: a transient
        # connector outage / expired token would then look like "this account
        # has zero leased hosts", which the discovery layer cannot distinguish
        # from a real empty result (and which defeats mngr's mark-UNKNOWN-on-
        # provider-failure safeguard). Let it propagate -- this method already
        # raises (via _require_account), so callers tolerate it.
        #
        # Narrow the propagated type by cause so consumers can tell "the
        # connector is unreachable" apart from "auth/account problem": a
        # transport-level httpx failure (connection refused, DNS, timeout --
        # the flaky-wifi / connector-down case) becomes ProviderUnavailableError,
        # which recovery UIs treat as "don't bother restarting, just retry". A
        # connector status error (ImbueCloudConnectorError) or an auth failure
        # (ImbueCloudAuthError) keeps its own type and falls through to the
        # generic "can't reach your workspace" handling instead. The curated
        # user_help_text keeps ProviderUnavailableError from telling a cloud user
        # to "start Docker".
        try:
            token = self._get_access_token(account)
            self._leased_hosts_cache = self.client.list_hosts(token)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                self.name,
                f"could not reach Imbue Cloud: {exc}",
                user_help_text=(
                    "Check your internet connection and try again. If the problem persists, "
                    "Imbue Cloud may be temporarily unavailable."
                ),
            ) from exc
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
            host_state = derive_host_state_from_raw(raw)
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
                script = build_outer_listing_collection_script(
                    str(host_id),
                    host_dir,
                    self.mngr_ctx.config.prefix,
                    window_name=self.mngr_ctx.config.tmux.primary_window_name,
                )
                result = outer.execute_idempotent_command(script, timeout_seconds=60.0)
        except HostAuthenticationError as exc:
            logger.warning(
                "imbue_cloud[{}] outer SSH authentication failed for host {}: {}",
                self.name,
                host_id,
                exc,
            )
            return None, f"outer SSH authentication failed: {exc}", True
        except MngrError as exc:
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
        offline_field_generators: Mapping[str, Mapping[str, Callable[[DiscoveredAgent, HostDetails], Any]]]
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
            return super().get_host_and_agent_details(
                host_ref,
                agent_refs,
                field_generators=field_generators,
                offline_field_generators=offline_field_generators,
                on_error=on_error,
            )
        resolved_offline_field_generators = offline_field_generators or {}
        raw = self._listing_raw_cache.get(host_id)
        if raw is None:
            # Discovery wasn't run for this host (rare; e.g. an explicit
            # detail call without going through `mngr list`); fall back.
            return self._build_offline_details_from_lease(
                host_ref, agent_refs, lease, "discovery did not run", resolved_offline_field_generators
            )
        outer_error = raw.get("outer_ssh_error")
        if outer_error is not None:
            return self._build_offline_details_from_lease(
                host_ref, agent_refs, lease, str(outer_error), resolved_offline_field_generators
            )
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
                build_agent_details_from_offline_ref(agent_ref, host_details, resolved_offline_field_generators)
                for agent_ref in agent_refs
            ]
        return host_details, agent_details_list

    def _ensure_outer_host_key_known(self, lease: LeasedHostInfo) -> None:
        """Pin the outer (VPS-root) sshd's host key the connector recorded, if not already present.

        ``outer_host_for`` connects with strict host-key checking. The outer sshd
        is reached at ``lease.ssh_port`` -- ``22`` for an OVH VPS (root sshd on
        :22) and the box-forwarded VM-root port for a slice (where ``:22`` is the
        bare-metal box's own sshd, not the VM). This recovers a fresh machine from
        the connector-provided key; it is add-if-absent so a locally-recorded key
        is never clobbered, and a None key (connector too old) is a no-op (the
        connection then fails strict checking, never trust-on-first-use). On local
        disk failure we log and proceed -- the caller's outer-SSH guard maps the
        resulting error to the lease-only fallback.
        """
        host_id = HostId(lease.host_id)
        try:
            self._ensure_host_key_pinned(host_id, lease.vps_address, lease.ssh_port, lease.outer_host_public_key)
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
        offline_field_generators: Mapping[str, Mapping[str, Callable[[DiscoveredAgent, HostDetails], Any]]],
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
            build_agent_details_from_offline_ref(agent_ref, host_details, offline_field_generators)
            for agent_ref in agent_refs
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
        host_state = derive_host_state_from_raw(raw)
        failure_reason = derive_offline_note_from_raw(raw)
        boot_time = timestamp_to_datetime(raw.get("btime"))
        uptime_seconds = raw.get("uptime_seconds")
        lock_mtime = raw.get("lock_mtime")
        # The lock file persists after release (its inode must stay stable across
        # local and remote holders), so its mtime alone does not indicate "held".
        # Use the real flock held-probe collected by the listing script.
        is_locked = bool(raw.get("is_lock_held"))
        locked_time = (
            datetime.fromtimestamp(lock_mtime, tz=timezone.utc) if is_locked and lock_mtime is not None else None
        )
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

        Mirrors ``mngr_vps``'s implementation -- the fields are
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

    def _build_host_object(self, lease: LeasedHostInfo, *, adopt_pre_baked_agent: bool = True) -> ImbueCloudHost:
        """Construct the ``ImbueCloudHost`` for a leased host.

        ``adopt_pre_baked_agent`` records whether the leased container still
        carries the bake's pre-provisioned agent state to adopt. The fast path
        (and discovery) leaves it True; the slow path passes False because it
        tore down the baked container and rebuilt it, so there is nothing to
        adopt -- ``pre_baked_agent_id=None`` then makes ``create_agent_*`` /
        ``provision_agent`` all fall through to mngr's standard full create.
        """
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

        # Pin the container's host key (and the VM-root key for later outer SSH)
        # from the connector-recorded key, add-if-absent so a locally-recorded
        # slow-path rebuilt key is never clobbered. Recovers a fresh machine
        # without any scan; a None key (old connector) leaves strict checking to
        # fail rather than falling back to trust-on-first-use.
        self._ensure_host_key_pinned(host_id, vps_address, lease.ssh_port, lease.outer_host_public_key)
        known_hosts_path = self._ensure_host_key_pinned(
            host_id, vps_address, container_ssh_port, lease.container_host_public_key
        )

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
            pre_baked_agent_id=agent_id if adopt_pre_baked_agent else None,
            lease_db_id=host_db_id,
        )
        self._evict_cached_host(host_id, replacement=host)
        return host

    def get_host(
        self,
        host: HostId | HostName,
    ) -> HostInterface:
        """Resolve a leased host, returning an offline host when its container is stopped.

        Mirrors ``VpsDockerProvider.get_host``: a leased host whose inner
        container is stopped must surface as an ``OfflineHost`` so that
        ``ensure_host_started`` routes ``mngr start`` through ``start_host``
        (which re-bootstraps the container's SSH). Returning an online ``Host``
        unconditionally -- as this did before -- makes the start command skip
        ``start_host`` and SSH straight into the dead container, leaving a
        stopped leased mind unrecoverable.
        """
        for entry in self._list_leased_hosts_cached():
            is_match = (isinstance(host, HostId) and entry.host_id == str(host)) or (
                isinstance(host, HostName) and entry.host_name == str(host)
            )
            if is_match:
                host_id = HostId(entry.host_id)
                if self._is_container_running(host_id):
                    return self._build_host_object(entry)
                return self.to_offline_host(host_id)
        raise HostNotFoundError(self.name, host)

    def _is_container_running(self, host_id: HostId) -> bool:
        """Return True iff the leased container is running on its outer VPS.

        Probed over the outer root SSH, which works independently of the
        container's own sshd. When the per-host key is not on this machine
        (e.g. the host was leased elsewhere), the outer cannot be opened, so we
        cannot prove the container is down and report it as running -- preserving
        the prior always-online behavior for that path. A container that no
        longer exists (lease torn down out from under us) reports as not running.
        """
        private_key_path, _ = self._host_keypair_paths(host_id)
        if not private_key_path.exists():
            return True
        with self.outer_host_for(host_id) as outer:
            assert outer is not None
            container_id = self._resolve_container_id_on_outer(outer, host_id)
            if container_id is None:
                return False
            return docker_inspect_running(outer, container_id)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Build an OfflineHost from the connector's lease metadata.

        The lease has no certified host data (that lives on the host itself,
        readable only via SSH). For the offline path used by ``mngr list``
        when SSH fails, we synthesize a minimal ``CertifiedHostData`` from
        the lease so the listing layer can still produce a row.
        """
        lease = self._find_leased(host_id)
        if lease is None:
            raise HostNotFoundError(self.name, host_id)
        now = datetime.now(timezone.utc)
        certified_host_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=lease.host_name,
            created_at=now,
            updated_at=now,
        )
        return make_readable_offline_host(
            OfflineHost(
                id=host_id,
                certified_host_data=certified_host_data,
                provider_instance=self,
                mngr_ctx=self.mngr_ctx,
            )
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
        """Lease a pool host and return it, via the fast (adopt) or slow (rebuild) path.

        The ``fast_mode`` build arg selects the path (default
        :data:`~imbue.mngr_imbue_cloud.primitives.DEFAULT_FAST_MODE`):

        - ``fast_mode=require`` -- the fast path. Lease a pool host whose
          ``attributes`` JSONB row exactly matches the requested attributes and
          adopt its pre-baked ``system-services`` agent (no transfer, minimal
          provision). If no exact match exists, raise
          ``FastPathUnavailableError`` so the caller can fall back.
        - ``fast_mode=prevent`` -- the slow path. Lease any adequately-sized
          available host (resource attributes only; ``repo_branch_or_tag`` /
          ``repo_url`` are dropped), destroy its baked container, and rebuild
          the host from scratch via the shared ``mngr_vps`` setup path,
          so mngr's standard create pipeline then does full client-side setup.

        Two address forms work for both paths:
          - ``...@.imbue_cloud_alice`` -- the per-account instance carries the
            account in ``config.account``.
          - ``...@.imbue_cloud -b account=alice@imbue.com`` -- the default
            instance takes the account from build args.

        Once a lease is obtained, any failure during the remaining setup
        releases the lease back to the pool before re-raising -- the client
        owns the machine the moment the connector marks it ``leased``.
        """
        if snapshot is not None:
            raise SnapshotsNotSupportedError(self.name)
        try:
            parsed = parse_imbue_cloud_build_args(build_args)
        except ValueError as exc:
            raise MngrError(f"Invalid build_args for imbue_cloud lease: {exc}") from exc

        account = self._require_account(parsed.account_override)
        token = self._get_access_token(account)

        match parsed.fast_mode:
            case FastMode.REQUIRE:
                # The fast path adopts the pre-baked pool-host container as-is. It
                # can tolerate start args the baked container already carries (the
                # pool_host template's docker run flags -- they are already in
                # effect), keeping the fast and slow paths in sync. It cannot honor
                # an --image swap or any other start arg, which would require a
                # rebuild via fast_mode=prevent.
                unsupported_start_args = tuple(
                    arg for arg in (start_args or ()) if arg not in FAST_PATH_ADOPTABLE_START_ARGS
                )
                if image is not None or unsupported_start_args:
                    # Name only the actual offender(s) so the message stays accurate
                    # whether it was an --image, unsupported start args, or both.
                    rejected_reasons: list[str] = []
                    if image is not None:
                        rejected_reasons.append(f"--image={image!r}")
                    if unsupported_start_args:
                        rejected_reasons.append(f"start args {list(unsupported_start_args)}")
                    raise MngrError(
                        "imbue_cloud fast_mode=require adopts the pre-baked agent as-is, so it cannot apply "
                        + " and ".join(rejected_reasons)
                        + ". Use fast_mode=prevent to rebuild."
                    )
                return self._create_host_fast_path(
                    name=name,
                    attributes=_resolve_fast_path_attributes(parsed.attributes),
                    token=token,
                    region=parsed.region,
                )
            case FastMode.PREVENT:
                return self._create_host_slow_path(
                    name=name,
                    attributes=parsed.attributes,
                    token=token,
                    image=image,
                    tags=tags,
                    start_args=start_args,
                    lifecycle=lifecycle,
                    known_hosts=known_hosts,
                    authorized_keys=authorized_keys,
                    passthrough_build_args=parsed.passthrough_build_args,
                    region=parsed.region,
                )
            case _ as unreachable:
                assert_never(unreachable)

    def _create_host_fast_path(
        self,
        *,
        name: HostName,
        attributes: LeaseAttributes,
        token: SecretStr,
        region: str | None,
    ) -> Host:
        """Lease an exact-attribute pool host and adopt its pre-baked agent.

        Raises ``FastPathUnavailableError`` (not ``ImbueCloudLeaseUnavailableError``)
        when no exact match exists, so the distinct signal lets a caller fall
        back to the slow path.
        """
        logger.info("imbue_cloud[{}] FAST PATH: leasing exact-attribute pool host for {!r}", self.name, str(name))
        tmp_private_key, tmp_public_key, public_key_text = self._prepare_pending_keypair()
        try:
            lease_result = self.client.lease_host(
                token,
                attributes,
                public_key_text,
                str(name),
                region=region,
            )
        except ImbueCloudLeaseUnavailableError as exc:
            self._discard_pending_keypair(tmp_private_key, tmp_public_key)
            raise FastPathUnavailableError(
                f"No pool host exactly matches the requested attributes for the fast (adopt) path: {exc}"
            ) from exc
        self.reset_caches()

        host_id = HostId(lease_result.host_id)
        with self._release_lease_on_failure(token, str(lease_result.host_db_id), host_id, "fast-path setup"):
            # Install the keypair inside the guard: the connector has already
            # marked the host leased, so a failure here (e.g. an OSError while
            # moving the key files) must still release the lease.
            final_private_key, _final_public_key = self._install_leased_keypair(
                host_id, tmp_private_key, tmp_public_key
            )
            self._persist_lease_meta(host_id, lease_result)
            # Wait for the leased container's sshd to be ready before we hand the
            # host back to mngr's create pipeline (which SSHes in immediately).
            wait_for_sshd(lease_result.vps_address, lease_result.container_ssh_port, _SSH_WAIT_TIMEOUT_SECONDS)
            # Pin the baked VM-root + container host keys the connector recorded
            # (strict host-key checking, no trust-on-first-use). Fail closed if the
            # connector did not return them (too old, or the host-key backfill has
            # not run) rather than silently scanning.
            if not lease_result.outer_host_public_key or not lease_result.container_host_public_key:
                raise MngrError(
                    f"lease of host {host_id} returned no pinned SSH host keys; upgrade the connector and run the "
                    "one-time `mngr imbue_cloud admin` host-key backfill"
                )
            self._record_host_key(
                host_id, lease_result.vps_address, lease_result.ssh_port, lease_result.outer_host_public_key
            )
            known_hosts_path = self._record_host_key(
                host_id,
                lease_result.vps_address,
                lease_result.container_ssh_port,
                lease_result.container_host_public_key,
            )
            # The pool host's ``/mngr/data.json`` was baked with a placeholder
            # host name; rewrite it to the user-supplied name so the FCT
            # bootstrap inherits the user's chosen workspace name.
            _rewrite_container_host_name(
                vps_address=lease_result.vps_address,
                container_ssh_port=lease_result.container_ssh_port,
                private_key_path=final_private_key,
                known_hosts_path=known_hosts_path,
                new_host_name=str(name),
            )
            host = self._build_host_object(self._leased_info_from_result(lease_result))
        logger.info(
            "imbue_cloud[{}] FAST PATH: adopted pre-baked agent {} on leased host {}",
            self.name,
            lease_result.agent_id,
            host_id,
        )
        return host

    def _create_host_slow_path(
        self,
        *,
        name: HostName,
        attributes: LeaseAttributes,
        token: SecretStr,
        image: ImageReference | None,
        tags: Mapping[str, str] | None,
        start_args: Sequence[str] | None,
        lifecycle: HostLifecycleOptions | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
        passthrough_build_args: tuple[str, ...],
        region: str | None,
    ) -> Host:
        """Lease any available host (relaxed attributes), nuke its container, and rebuild it.

        The rebuilt container keeps the lease's pre-baked ``host_id`` /
        ``agent_id`` (so mngr identity stays aligned with the connector's lease
        row) but has no on-disk agent state, so the returned ``ImbueCloudHost``
        falls through to mngr's standard full create + provision pipeline --
        exactly as if this were a fresh OVH host.
        """
        relaxed_attributes = attributes.relaxed()
        logger.info(
            "imbue_cloud[{}] SLOW PATH: no fast match requested; leasing any available host "
            "(relaxed attributes {}) to rebuild for {!r}",
            self.name,
            relaxed_attributes.to_request_dict(),
            str(name),
        )
        tmp_private_key, tmp_public_key, public_key_text = self._prepare_pending_keypair()
        try:
            # Region constraints are NOT relaxed: a hard ``region`` requirement
            # still applies to the rebuilt host.
            lease_result = self.client.lease_host(
                token,
                relaxed_attributes,
                public_key_text,
                str(name),
                region=region,
            )
        except ImbueCloudLeaseUnavailableError:
            # Genuinely no available host in the pool -- nothing was leased, so
            # there is nothing to release. Surface the pool-exhausted signal.
            self._discard_pending_keypair(tmp_private_key, tmp_public_key)
            raise
        self.reset_caches()

        host_id = HostId(lease_result.host_id)
        with self._release_lease_on_failure(token, str(lease_result.host_db_id), host_id, "slow-path rebuild"):
            # Install the keypair inside the guard: the connector has already
            # marked the host leased, so a failure here (e.g. an OSError while
            # moving the key files) must still release the lease.
            _final_private_key, final_public_key = self._install_leased_keypair(
                host_id, tmp_private_key, tmp_public_key
            )
            self._persist_lease_meta(host_id, lease_result)
            per_host_public_key = final_public_key.read_text().strip()
            rebuilt_container_public_key = self._rebuild_leased_container(
                host_id=host_id,
                name=name,
                lease_result=lease_result,
                per_host_public_key=per_host_public_key,
                image=image,
                tags=tags,
                start_args=start_args,
                lifecycle=lifecycle,
                known_hosts=known_hosts,
                authorized_keys=authorized_keys,
                passthrough_build_args=passthrough_build_args,
            )
            # Pin the VM-root host key (deterministic, unchanged by the rebuild,
            # from the connector) and the rebuilt container's freshly-generated
            # host key (known locally from the rebuild provider). The connector's
            # recorded *initial* container key is intentionally NOT used here -- the
            # rebuild replaced it -- and there is no write-back: this container is
            # the user's now. No scan, no trust-on-first-use.
            if lease_result.outer_host_public_key:
                self._record_host_key(
                    host_id, lease_result.vps_address, lease_result.ssh_port, lease_result.outer_host_public_key
                )
            self._record_host_key(
                host_id, lease_result.vps_address, lease_result.container_ssh_port, rebuilt_container_public_key
            )
            # The container was torn down and rebuilt -- there is no baked agent
            # state to adopt, so don't mark the host as pre-baked. This makes
            # mngr run its standard full create + provision (matching this
            # method's "fresh OVH host" contract) instead of the adopt path.
            host = self._build_host_object(self._leased_info_from_result(lease_result), adopt_pre_baked_agent=False)
        logger.info(
            "imbue_cloud[{}] SLOW PATH: rebuilt container on leased host {} (lease {}); "
            "mngr will now run full client-side setup",
            self.name,
            host_id,
            lease_result.host_db_id,
        )
        return host

    def _rebuild_leased_container(
        self,
        *,
        host_id: HostId,
        name: HostName,
        lease_result: LeaseResult,
        per_host_public_key: str,
        image: ImageReference | None,
        tags: Mapping[str, str] | None,
        start_args: Sequence[str] | None,
        lifecycle: HostLifecycleOptions | None,
        known_hosts: Sequence[str] | None,
        authorized_keys: Sequence[str] | None,
        passthrough_build_args: tuple[str, ...],
        # the rebuilt container's (freshly-generated) host public key, to pin
    ) -> str:
        """Tear down the leased VPS's baked container and rebuild it from the FCT Dockerfile.

        Delegates both teardown and rebuild to the single canonical
        ``mngr_vps`` setup path, run over the root SSH the lease granted.
        The per-host public key is added to the rebuilt container's
        ``authorized_keys`` so the returned ``ImbueCloudHost`` (which uses the
        per-host key) can reach it. Returns the rebuilt container's host public
        key (the rebuild provider's own, known locally -- no scan) so the caller
        can pin it.
        """
        # A slice's container is reached at a box-forwarded host port that differs
        # from the in-VM publish port, so its rebuild must use the slice provider
        # (which splits publish vs connect ports) rather than the plain vps_docker
        # one. Detected by the lease's container port differing from the standard
        # publish port -- true only for slices (forwarded ports), never OVH VPSes.
        is_slice = lease_result.container_ssh_port != self.config.container_ssh_port
        delegated_provider: VpsProvider = (
            build_slice_rebuild_provider(
                name=self.name, config=self.config, mngr_ctx=self.mngr_ctx, lease_result=lease_result
            )
            if is_slice
            else build_delegated_vps_provider(name=self.name, config=self.config, mngr_ctx=self.mngr_ctx)
        )
        # The VPS root host key (port 22 for OVH; the box-forwarded VM-root port
        # for a slice) feeds the rebuilt host's record AND is pinned in the
        # delegated provider's own known_hosts, so its outer connections -- e.g.
        # the certified-data sync callback -- pass strict host-key checking
        # instead of failing on a missing entry. It is the deterministic key the
        # connector recorded at bake time (no scan); the container rebuild does
        # not change it.
        vps_host_public_key = lease_result.outer_host_public_key or ""
        if vps_host_public_key:
            delegated_provider.record_outer_host_key(
                lease_result.vps_address, lease_result.ssh_port, vps_host_public_key
            )
        combined_authorized_keys = tuple(authorized_keys or ()) + (per_host_public_key,)
        with self._outer_for_leased_vps(host_id, lease_result) as outer:
            delegated_provider.teardown_container_on_existing_vps(outer, host_id)
            # Re-apply the full idempotent host setup on the leased VPS before
            # rebuilding, so a host baked with an old version (or before runsc
            # existed) is brought up to current: pinned Docker, runsc, sshd
            # tuning, base packages. This is the single source of truth shared
            # with the OVH bake + cloud-init backends. Runs after teardown (no
            # container running, so a Docker upgrade/restart is safe) and before
            # the rebuild; a failure raises and aborts the create. ``runsc`` is
            # installed when the provider is configured for it (minds writes
            # ``install_gvisor_runtime=true`` into the per-account block), so the
            # rebuilt container can run under ``--runtime runsc``. qemu purge is
            # enabled because the pool is OVH-backed (a no-op when no qemu).
            # Skipped for slices: the lima VM is already provisioned (Docker +
            # sshd from the bake's provision script) and uses runc (the VM is the
            # isolation boundary), so the OVH/runsc/qemu host-setup does not apply.
            if not is_slice:
                apply_host_setup_on_outer(
                    outer,
                    install_gvisor_runtime=self.config.install_gvisor_runtime,
                    is_qemu_purge_enabled=True,
                )
            delegated_provider.create_host_on_existing_vps(
                outer=outer,
                host_id=host_id,
                name=name,
                vps_ip=lease_result.vps_address,
                vps_instance_id=VpsInstanceId(str(lease_result.host_db_id)),
                vps_ssh_key_id="",
                vps_host_public_key=vps_host_public_key,
                region="imbue-cloud-pool",
                plan="imbue-cloud-pool",
                image=image,
                tags=tags,
                build_args=passthrough_build_args,
                start_args=start_args,
                lifecycle=lifecycle,
                known_hosts=known_hosts,
                authorized_keys=combined_authorized_keys,
            )
        # The rebuilt container's host key is the delegated provider's own
        # (injected into the container at rebuild), so it is known locally without
        # any scan. Read it via the provider's public host-key accessor (second
        # element is the container key) rather than the private keypair method.
        _outer_host_public_key, rebuilt_container_public_key = delegated_provider.get_ssh_host_public_keys(host_id)
        if not rebuilt_container_public_key:
            raise MngrError(
                f"rebuilt container for host {host_id} did not surface its sshd host public key; cannot pin it"
            )
        return rebuilt_container_public_key

    @contextmanager
    def _outer_for_leased_vps(self, host_id: HostId, lease_result: LeaseResult) -> Iterator[OuterHostInterface]:
        """Open an outer host (root@vps:ssh_port) for the leased VPS via the per-host key.

        Pins the VPS root host key the connector recorded (the deterministic
        VM-root/VPS-root key) so strict host-key checking succeeds on the very
        first connection -- no scan, no trust-on-first-use.
        """
        private_key_path, _ = self._host_keypair_paths(host_id)
        known_hosts_path = self._ensure_host_key_pinned(
            host_id, lease_result.vps_address, lease_result.ssh_port, lease_result.outer_host_public_key
        )
        pyinfra_host = create_pyinfra_host(
            hostname=lease_result.vps_address,
            port=lease_result.ssh_port,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            ssh_user=lease_result.ssh_user,
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

    @contextmanager
    def _release_lease_on_failure(
        self,
        token: SecretStr,
        host_db_id: str,
        host_id: HostId,
        phase: str,
    ) -> Iterator[None]:
        """Release the lease back to the pool if the wrapped setup fails.

        The client owns the machine the moment the connector marks it leased,
        so any failure after a successful lease must return the host so it is
        not leaked. No data wipe is attempted -- nothing sensitive exists on a
        freshly-leased host yet, and it may not even be reachable.

        Implemented with a success flag + ``finally`` (rather than ``except``)
        so the wrapped exception propagates untouched while the lease is still
        released on any non-success exit.
        """
        is_success = False
        try:
            yield
            is_success = True
        finally:
            if not is_success:
                logger.warning(
                    "imbue_cloud[{}] {} failed for host {}; releasing lease {} back to the pool",
                    self.name,
                    phase,
                    host_id,
                    host_db_id,
                )
                self._release_lease_quietly(token, host_db_id)
                self._cleanup_local_host_state(host_id)

    def _release_lease_quietly(self, token: SecretStr, host_db_id: str) -> None:
        """Best-effort release of a lease; logs (never raises) on failure.

        Used only by the create-rollback path, where the *original* failure is
        what the operator needs to see -- a release problem here must not mask
        it, so we catch and log rather than propagate.
        """
        try:
            self.client.release_host(token, host_db_id)
        except ImbueCloudConnectorError as exc:
            logger.warning("imbue_cloud[{}] release of lease {} did not succeed: {}", self.name, host_db_id, exc)

    def _prepare_pending_keypair(self) -> tuple[Path, Path, str]:
        """Generate a per-lease SSH keypair in a temp dir (host_id not yet known)."""
        leases_dir = self._provider_data_dir() / "leases"
        leases_dir.mkdir(parents=True, exist_ok=True)
        tmp_key_dir = leases_dir / f"pending-{int(time.time() * 1000)}"
        tmp_key_dir.mkdir(parents=True, exist_ok=True)
        tmp_private_key, tmp_public_key = save_ssh_keypair(tmp_key_dir, "ssh_key")
        return tmp_private_key, tmp_public_key, tmp_public_key.read_text().strip()

    def _discard_pending_keypair(self, tmp_private_key: Path, tmp_public_key: Path) -> None:
        """Remove a pending keypair + its temp dir when the lease never happened."""
        for path in (tmp_private_key, tmp_public_key):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            tmp_private_key.parent.rmdir()
        except OSError:
            pass

    def _install_leased_keypair(
        self,
        host_id: HostId,
        tmp_private_key: Path,
        tmp_public_key: Path,
    ) -> tuple[Path, Path]:
        """Move a pending keypair into the canonical ``hosts/<host_id>/`` location."""
        host_state_dir = self._host_state_dir(host_id)
        host_state_dir.mkdir(parents=True, exist_ok=True)
        final_private_key = host_state_dir / "ssh_key"
        final_public_key = host_state_dir / "ssh_key.pub"
        tmp_private_key.replace(final_private_key)
        tmp_public_key.replace(final_public_key)
        final_private_key.chmod(0o600)
        # Best-effort cleanup of the pending dir; harmless if peers remain.
        try:
            tmp_private_key.parent.rmdir()
        except OSError:
            pass
        return final_private_key, final_public_key

    def _persist_lease_meta(self, host_id: HostId, lease_result: LeaseResult) -> None:
        """Persist lease metadata so later commands find host_db_id without the connector."""
        lease_meta_path = self._host_state_dir(host_id) / "lease.json"
        lease_meta_path.write_text(json.dumps(lease_result.model_dump(), indent=2, default=str))

    def _record_host_key(
        self,
        host_id: HostId,
        hostname: str,
        port: int,
        public_key: str,
    ) -> Path:
        """Authoritatively pin ``public_key`` for ``hostname:port`` (replacing any prior entry).

        Used at lease/rebuild time when we hold the definitive key -- the
        connector's recorded key for an adopted container/VM-root, or the rebuild
        provider's own key for a slow-path-rebuilt container. Returns the
        known_hosts path. No scan, no trust-on-first-use.
        """
        known_hosts_path = self._host_known_hosts_path(host_id)
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        if not known_hosts_path.exists():
            known_hosts_path.touch()
        add_host_to_known_hosts(known_hosts_path, hostname, port, public_key)
        return known_hosts_path

    def _ensure_host_key_pinned(
        self,
        host_id: HostId,
        hostname: str,
        port: int,
        public_key: str | None,
    ) -> Path:
        """Pin ``public_key`` for ``hostname:port`` only if no entry already exists.

        Add-if-absent: an existing entry for this host:port is left untouched, so a
        slow-path-rebuilt container's locally-recorded host key is never clobbered
        by the connector's (stale) initial key. Used by later operations to recover
        a fresh machine from the connector-provided key. A None key (connector too
        old to return it) is a no-op -- the connection then fails strict checking
        rather than falling back to trust-on-first-use.
        """
        known_hosts_path = self._host_known_hosts_path(host_id)
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        if not known_hosts_path.exists():
            known_hosts_path.touch()
        if public_key:
            host_pattern = format_as_known_hosts_address(hostname, port)
            # Match a known_hosts *line* whose leading field is exactly this
            # host:port (mirrors add_host_to_known_hosts / clear_host_from_known_hosts).
            # A bare-hostname pattern (default port) is a substring of the bracketed
            # ``[host]:port`` form, so a plain ``in`` substring test would wrongly
            # treat the outer (:22) key as already present when only a container
            # ([host]:2222) entry exists, silently skipping the pin.
            entry_prefix = f"{host_pattern} "
            already_present = any(line.startswith(entry_prefix) for line in known_hosts_path.read_text().splitlines())
            if not already_present:
                add_host_to_known_hosts(known_hosts_path, hostname, port, public_key)
        return known_hosts_path

    def _leased_info_from_result(self, lease_result: LeaseResult) -> LeasedHostInfo:
        """Build a ``LeasedHostInfo`` from a fresh lease response."""
        return LeasedHostInfo(
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
            outer_host_public_key=lease_result.outer_host_public_key,
            container_host_public_key=lease_result.container_host_public_key,
        )

    def _resolve_container_id_on_outer(self, outer: OuterHostInterface, host_id: HostId) -> str | None:
        """Look up the docker container id for the given inner host on its outer VPS.

        Returns None when no container with that label exists. Containers are
        identified by ``com.imbue.mngr.host-id=<host_id>`` (the canonical
        ``LABEL_HOST_ID`` from ``mngr_vps``).
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
        """Start the previously-stopped docker container, relaunch its sshd, and return the Host.

        A bare ``docker start`` is not enough to bring a leased mind back: the
        in-container sshd is launched via ``docker exec`` (the container's CMD is
        just a sleep), so the sshd *process* does not survive the stop. The
        container filesystem -- including the per-host authorized key and the
        served host key -- is preserved across a ``docker stop``/``docker
        start``, so only sshd needs re-establishing; without it the subsequent
        ``mngr start`` SSH into the container hangs until timeout and the mind is
        left dead and UI-unrecoverable. So, over the outer root SSH (which works
        independently of the container's sshd), we relaunch sshd and wait for it
        to accept connections.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        leased = self._find_leased(host_id)
        if leased is None:
            raise HostNotFoundError(self.name, host_id)
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
            # The container's CMD is just a sleep, so a freshly started container
            # is not running sshd (it is launched via ``docker exec``, never the
            # entrypoint); launch it. Otherwise the wait below (and the later
            # ``mngr start`` SSH) would hang until timeout and the mind would be
            # unrecoverable.
            start_container_sshd(outer, container_id)
            self._wait_for_container_sshd(leased)
        return self._build_host_object(leased)

    def _wait_for_container_sshd(self, leased: LeasedHostInfo) -> None:
        """Wait for the container's sshd to accept connections on the leased VPS's port."""
        wait_for_sshd(leased.vps_address, leased.container_ssh_port, _SSH_WAIT_TIMEOUT_SECONDS)

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Wipe user data on the leased VPS and release the lease back to the pool.

        Three phases, in order, so the data is unreachable to the next user
        before the lease is returned to the pool:

        1. Stop + remove the workspace container, drop the per-host named
           docker volume, delete the per-host btrfs subvolume under
           ``/mngr-btrfs/``, run ``docker system prune -a -f --volumes``,
           and wipe ``/root`` and ``/tmp`` (preserving ``authorized_keys``
           so the pool-management ssh path keeps working through cleanup).
        2. Release the lease via the connector's ``/hosts/{id}/release``
           endpoint -- the row flips to ``released`` and gets picked up by
           ``cleanup_released_hosts.py`` later for VPS-destroy.
        3. Drop local per-host state (ssh keys, known_hosts, cached records).

        Best-effort across steps: a failed wipe still proceeds to release
        (because a stuck VPS would otherwise leak a paid lease indefinitely),
        and the data wipe is non-gating because the VPS is destroyed wholesale
        by ``cleanup_released_hosts.py`` after release regardless. A failed
        release, however, means the paid lease is leaked -- it is recorded as a
        ``HOST_RESOURCE_REMAINS`` failure and local cleanup is intentionally
        skipped (removing local SSH keys for a host that was never released
        would make mngr unable to reach the still-running VPS). Operators see
        warnings for each partial failure.

        Best-effort: real cleanup failures (resources left behind) are raised
        as a ``CleanupFailedGroup``; returns normally on full success or benign
        "already gone" outcomes. See specs/cleanup-error-aggregation.md.

        Use ``mngr stop`` (-> ``stop_host``) instead when you intend to
        resume the workspace later on the same VPS -- that path preserves
        the lease and the on-disk data.
        """
        self._wipe_and_release_pool_host(host)

    def delete_host(self, host: HostInterface) -> None:
        """Same as ``destroy_host``; provided for the GC code path.

        mngr's GC calls ``delete_host`` after the destroyed-host grace
        period. Since ``destroy_host`` is now terminal (wipes + releases
        the lease immediately), ``delete_host`` is functionally a re-run
        of the same flow -- it's a no-op for an already-released lease
        and a recovery path if a previous destroy crashed mid-wipe.
        """
        # delete_host returns None per the interface; cleanup failures are surfaced through
        # destroy_host, not here. GC calls delete_host after the grace period as a recovery
        # re-run, so a residual leftover-resource failure must not abort the GC sweep -- the
        # shared flow's CleanupFailedGroup is logged and swallowed.
        try:
            self._wipe_and_release_pool_host(host)
        except CleanupFailedGroup as group:
            logger.warning("Cleanup left resources behind while deleting host {}: {}", host.id, group)

    def _wipe_and_release_pool_host(self, host: HostInterface | HostId) -> None:
        """Shared implementation for ``destroy_host`` and ``delete_host``.

        See ``destroy_host`` for the contract. Split out so both entry
        points run identically; both are now terminal for imbue_cloud.

        Raises a ``CleanupFailedGroup`` carrying the real cleanup failures
        (resources left behind); returns normally otherwise. The wipe step is
        non-gating (warn-only): the leased VPS is destroyed wholesale by
        ``cleanup_released_hosts.py`` after the release, so residual data on it
        is not a leaked resource from mngr's accounting. A failed release leaks
        the paid lease and is recorded as ``HOST_RESOURCE_REMAINS``.
        """
        failures: list[CleanupFailure] = []
        host_id = host.id if isinstance(host, HostInterface) else host
        leased = self._find_leased(host_id)
        host_db_id: str | None = None
        if isinstance(host, HostInterface):
            host_db_id = self._resolve_host_db_id(host, host_id)
        if host_db_id is None and leased is not None:
            host_db_id = str(leased.host_db_id)

        if leased is None and host_db_id is None:
            logger.warning(
                "destroy_host: no lease record for host {} (already released?); running local cleanup only.",
                host_id,
            )
            self._cleanup_local_host_state(host_id)
            if failures:
                raise CleanupFailedGroup.from_failures(failures)
            return

        if leased is not None:
            try:
                with self.outer_host_for(host_id) as outer:
                    assert outer is not None
                    script = build_pool_host_wipe_script(host_id)
                    result = outer.execute_idempotent_command(script, timeout_seconds=300.0)
                    if not result.success:
                        # The script exits 0 at the end on purpose; a non-zero
                        # exit indicates the SSH transport itself or shell-
                        # invocation framing failed. Log + proceed -- release
                        # is the gating step regardless.
                        logger.warning(
                            "destroy_host: wipe script returned non-zero for {} "
                            "(stderr={!r}); proceeding with release.",
                            host_id,
                            result.stderr.strip(),
                        )
                    else:
                        logger.debug("Wiped pool VPS data for host {}", host_id)
            except HostNotFoundError:
                logger.warning(
                    "destroy_host: SSH key for host {} is missing; cannot wipe data on VPS. Proceeding with release.",
                    host_id,
                )
            except MngrError as exc:
                logger.warning(
                    "destroy_host: data wipe failed for host {}: {}. Proceeding with release.",
                    host_id,
                    exc,
                )

        if host_db_id is not None:
            account = self._require_account()
            token = self._get_access_token(account)
            # release_host raises ImbueCloudConnectorError on failure (transport
            # error or non-2xx, e.g. the synchronous release returning 5xx when
            # the OVH cancel failed); the idempotent ``already_released`` case is
            # a 2xx and returns normally, so any exception here means the paid
            # lease is actually leaked. Record it as HOST_RESOURCE_REMAINS and
            # return WITHOUT running local cleanup -- cleaning up here would make
            # mngr "forget" a host that was never actually released (the old
            # silent-orphan bug) and drop the local SSH keys needed to reach the
            # still-running VPS.
            try:
                self.client.release_host(token, host_db_id)
            except ImbueCloudConnectorError as exc:
                logger.warning("Failed to release leased VPS for host {}: {}", host_id, exc)
                failures.append(
                    CleanupFailure(
                        category=CleanupFailureCategory.HOST_RESOURCE_REMAINS,
                        message=f"failed to release leased VPS for host {host_id}: {exc}",
                        host_id=host_id,
                    )
                )
                raise CleanupFailedGroup.from_failures(failures) from exc
        self._cleanup_local_host_state(host_id)
        if failures:
            raise CleanupFailedGroup.from_failures(failures)
        return

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
            raise HostNotFoundError(self.name, host_id)
        return f"outer:{self.name}:{leased.vps_address}"

    @contextmanager
    def outer_host_for(self, host_id: HostId) -> Iterator[OuterHostInterface | None]:
        """Open the outer host (the leased VPS itself, root@vps_address:ssh_port).

        Uses the per-host SSH key already on disk (the lease step authorized
        this key on both the container's sshd and the VPS root account). The
        outer sshd port is ``lease.ssh_port`` -- ``22`` for an OVH VPS, and
        the box-forwarded VM-root port for a slice (``:22`` on a slice's
        ``vps_address`` is the bare-metal box's own sshd, not the VM).
        """
        leased = self._find_leased(host_id)
        if leased is None:
            raise HostNotFoundError(self.name, host_id)
        private_key_path, _ = self._host_keypair_paths(host_id)
        if not private_key_path.exists():
            raise HostNotFoundError(self.name, host_id)

        known_hosts_path = self._host_known_hosts_path(host_id)
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        if not known_hosts_path.exists():
            known_hosts_path.touch()

        pyinfra_host = create_pyinfra_host(
            hostname=leased.vps_address,
            port=leased.ssh_port,
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
        # Build the online host object directly from the lease rather than via
        # get_host: the connector is needed regardless of the container's
        # running state, and get_host now returns an OfflineHost (which has no
        # connector) for a stopped container.
        host_id = host.id if isinstance(host, HostInterface) else host
        leased = self._find_leased(host_id)
        if leased is None:
            raise HostNotFoundError(self.name, host_id)
        return self._build_host_object(leased).connector.host


def _rm_tree(path: Path) -> None:
    """Recursively delete a path, raising the first OSError encountered."""
    if path.is_file() or path.is_symlink():
        path.unlink()
        return
    for child in path.iterdir():
        _rm_tree(child)
    path.rmdir()
