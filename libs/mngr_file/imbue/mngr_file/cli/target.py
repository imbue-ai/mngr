from pathlib import Path
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr_file.data_types import PathRelativeTo


@pure
def resolve_full_path(base_path: Path, user_path: str) -> Path:
    """Combine a base path with a user-provided path, respecting absolute paths."""
    parsed = Path(user_path)
    if parsed.is_absolute():
        return parsed
    return base_path / parsed


@pure
def _compute_agent_base_path(
    relative_to: PathRelativeTo,
    work_dir: Path | None,
    host_dir: Path,
    agent_id: AgentId,
) -> Path:
    match relative_to:
        case PathRelativeTo.WORK:
            # Callers guarantee a work_dir before requesting a WORK-relative base path
            # (resolve raises UserInputError when it is missing); STATE/HOST never read it.
            assert work_dir is not None, "work_dir is required for WORK-relative paths"
            return work_dir
        case PathRelativeTo.STATE:
            return get_agent_state_dir_path(host_dir, agent_id)
        case PathRelativeTo.HOST:
            return host_dir
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _is_volume_accessible_path(relative_to: PathRelativeTo) -> bool:
    """Whether the given relative_to mode produces paths under host_dir (accessible via volume)."""
    match relative_to:
        case PathRelativeTo.WORK:
            return False
        case PathRelativeTo.STATE:
            return True
        case PathRelativeTo.HOST:
            return True
        case _ as unreachable:
            assert_never(unreachable)


@pure
def compute_volume_path(
    relative_to: PathRelativeTo,
    agent_id: AgentId | None,
    user_path: str | None,
) -> str:
    """Compute the path within a volume for a given relative_to mode and user path.

    Volume paths are relative to the host_dir root. Returns a path string
    suitable for Volume.read_file() and Volume.listdir().
    """
    match relative_to:
        case PathRelativeTo.HOST:
            if user_path is None:
                return "."
            return user_path
        case PathRelativeTo.STATE:
            if agent_id is None:
                raise UserInputError("--relative-to state requires an agent target")
            base = f"agents/{agent_id}"
            if user_path is None:
                return base
            return f"{base}/{user_path}"
        case PathRelativeTo.WORK:
            raise UserInputError(
                "Cannot access work directory files when the host is offline. "
                "Use --relative-to state or --relative-to host instead."
            )
        case _ as unreachable:
            assert_never(unreachable)


class ResolveFileTargetResult(FrozenModel):
    """Result of resolving a file command target to access methods and base path."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    online_host: OnlineHostInterface | None = Field(default=None, description="Online host for direct access")
    volume: Volume | None = Field(default=None, description="Volume for offline access")
    base_path: Path | None = Field(
        default=None,
        description="Base path for resolving host-relative paths. None when the host is offline, "
        "since no host-relative base path exists (offline access goes through compute_volume_path).",
    )
    is_agent: bool = Field(description="Whether the target is an agent (vs a host)")
    agent_id: AgentId | None = Field(default=None, description="Agent ID if target is an agent")
    relative_to: PathRelativeTo = Field(description="Path resolution mode")

    @property
    def host(self) -> OnlineHostInterface:
        """Get the online host, raising if not available."""
        if self.online_host is None:
            raise MngrError(
                "Host is offline and this operation requires direct host access. "
                "Use --relative-to state or --relative-to host for offline access."
            )
        return self.online_host

    @property
    def host_base_path(self) -> Path:
        """Get the host-relative base path, raising if the host is offline (no such path exists)."""
        if self.base_path is None:
            raise MngrError(
                "Host is offline and this operation requires a host-relative base path. "
                "Use --relative-to state or --relative-to host for offline access."
            )
        return self.base_path

    @property
    def is_online(self) -> bool:
        return self.online_host is not None


def resolve_file_target(
    target: AgentOrHostAddress,
    mngr_ctx: MngrContext,
    relative_to: PathRelativeTo,
) -> ResolveFileTargetResult:
    """Resolve a TARGET argument to a host/volume and base path for file operations.

    Whether the target is an agent or a host is determined by ``target``'s
    runtime type (parsed by the standard ``AGENT_OR_HOST_ADDRESS`` Click
    param type, which uses :func:`parse_agent_or_host_address`). To target a
    host whose name shares the shape of a :class:`SafeName`, write ``@host``
    on the command line so the parser picks the host arm.

    When the target host is online, direct host access is used. When offline,
    falls back to volume access for paths under the host directory.

    Note that, unlike the standard single-agent flow used by connect/push/etc.,
    this resolver does **not** call into ``ensure_host_*`` after finding the
    agent: ``mngr file`` is allowed to operate against an offline host
    through the provider's volume backend, and forcing the host online would
    silently strip that capability.
    """
    if isinstance(target, AgentAddress):
        host_ref, agent_ref = find_one_agent(target, mngr_ctx)
        return _resolve_agent_target(
            discovered_host=host_ref,
            discovered_agent=agent_ref,
            mngr_ctx=mngr_ctx,
            relative_to=relative_to,
        )

    # Host target. filter_one_host raises if zero or multiple hosts match.
    if relative_to != PathRelativeTo.HOST and relative_to != PathRelativeTo.WORK:
        raise UserInputError(
            f"--relative-to {relative_to.value.lower()} is only valid for agent targets. "
            f"Host targets always use MNGR_HOST_DIR as the base path."
        )
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    host_ref = filter_one_host(target, list(agents_by_host.keys()))
    return _resolve_host_target(
        discovered_host=host_ref,
        mngr_ctx=mngr_ctx,
    )


def _get_host_access(
    provider: BaseProviderInstance,
    host_id: HostId,
    target_display_name: str,
) -> tuple[OnlineHostInterface | None, Volume | None]:
    """Get online host and/or volume access for a host, raising if neither is available."""
    # Try online access
    online_host: OnlineHostInterface | None = None
    try:
        host_interface = provider.get_host(host_id)
    except HostNotFoundError as err:
        # The host_id was just produced by discovery, so a missing host here means it was
        # destroyed in the interim; degrade to volume access but log at warning so the
        # degradation is visible. Any other MngrError (auth/config/provider bugs) propagates.
        logger.warning("Host {} not found; falling back to volume access: {}", host_id, err)
        host_interface = None

    if host_interface is not None and isinstance(host_interface, OnlineHostInterface):
        online_host = host_interface

    # Try volume access
    host_volume = provider.get_volume_for_host(host_id)
    volume: Volume | None = None
    if host_volume is not None:
        volume = host_volume.volume

    if online_host is None and volume is None:
        raise MngrError(
            f"{target_display_name} is offline and the provider does not support volume access. Cannot access files."
        )

    return online_host, volume


def _resolve_agent_target(
    discovered_host: DiscoveredHost,
    discovered_agent: DiscoveredAgent,
    mngr_ctx: MngrContext,
    relative_to: PathRelativeTo,
) -> ResolveFileTargetResult:
    with log_span("Getting access for agent target"):
        provider = get_provider_instance(discovered_host.provider_name, mngr_ctx)

    online_host, volume = _get_host_access(
        provider=provider,
        host_id=discovered_host.host_id,
        target_display_name=f"Host for agent '{discovered_agent.agent_name}'",
    )

    # When online, get work_dir from the host's agent list
    work_dir: Path | None = None
    if online_host is not None:
        for agent_ref in online_host.discover_agents():
            if agent_ref.agent_id == discovered_agent.agent_id:
                work_dir = agent_ref.work_dir
                break

    # When offline, use discovered data for work_dir
    if work_dir is None:
        work_dir = discovered_agent.work_dir

    if work_dir is None and relative_to == PathRelativeTo.WORK:
        raise UserInputError(f"Could not determine work directory for agent: {discovered_agent.agent_name}")

    # For offline + work_dir relative, we can't use volume
    if online_host is None and not _is_volume_accessible_path(relative_to):
        raise UserInputError(
            "Host is offline. Work directory files are not accessible via volume. "
            "Use --relative-to state or --relative-to host for offline access."
        )

    # A host-relative base_path only exists when the host is online. Offline access never reads
    # it (get/put gate on is_online; list resolves via compute_volume_path()), so leave it None
    # rather than fabricating a fake path that could silently flow downstream.
    base_path: Path | None = None
    if online_host is not None:
        base_path = _compute_agent_base_path(
            relative_to=relative_to,
            work_dir=work_dir,
            host_dir=online_host.host_dir,
            agent_id=discovered_agent.agent_id,
        )
    logger.debug("Resolved agent target: base_path={}, is_online={}", base_path, online_host is not None)

    return ResolveFileTargetResult(
        online_host=online_host,
        volume=volume,
        base_path=base_path,
        is_agent=True,
        agent_id=discovered_agent.agent_id,
        relative_to=relative_to,
    )


def _resolve_host_target(
    discovered_host: DiscoveredHost,
    mngr_ctx: MngrContext,
) -> ResolveFileTargetResult:
    with log_span("Getting access for host target"):
        provider = get_provider_instance(discovered_host.provider_name, mngr_ctx)

    online_host, volume = _get_host_access(
        provider=provider,
        host_id=discovered_host.host_id,
        target_display_name=f"Host '{discovered_host.host_name}'",
    )

    # base_path only exists when online; offline host listing resolves via compute_volume_path().
    base_path = online_host.host_dir if online_host is not None else None

    logger.debug("Resolved host target: base_path={}, is_online={}", base_path, online_host is not None)

    return ResolveFileTargetResult(
        online_host=online_host,
        volume=volume,
        base_path=base_path,
        is_agent=False,
        agent_id=None,
        relative_to=PathRelativeTo.HOST,
    )
