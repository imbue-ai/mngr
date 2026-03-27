from pathlib import Path
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_all_hosts_and_agents
from imbue.mngr.api.find import find_all_matching_agents
from imbue.mngr.api.find import find_all_matching_hosts
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
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
    work_dir: Path,
    host_dir: Path,
    agent_id: AgentId,
) -> Path:
    match relative_to:
        case PathRelativeTo.WORK:
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
    base_path: Path = Field(description="Base path for resolving relative paths")
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
    def is_online(self) -> bool:
        return self.online_host is not None


def resolve_file_target(
    target_identifier: str,
    mngr_ctx: MngrContext,
    relative_to: PathRelativeTo,
) -> ResolveFileTargetResult:
    """Resolve a TARGET argument to a host/volume and base path for file operations.

    Tries agent resolution first, then host resolution. If both match, raises
    an error requiring disambiguation. If neither matches, raises an error.

    When the target host is online, direct host access is used. When offline,
    falls back to volume access for paths under the host directory.
    """
    with log_span("Discovering hosts and agents"):
        agents_by_host, _ = discover_all_hosts_and_agents(mngr_ctx, include_destroyed=False)

    all_hosts = list(agents_by_host.keys())

    # Find all matching agents and hosts
    matching_agents = find_all_matching_agents(target_identifier, agents_by_host)
    matching_hosts = find_all_matching_hosts(target_identifier, all_hosts)

    # Check for ambiguity within each type
    if len(matching_agents) > 1:
        raise UserInputError(
            f"Multiple agents found matching '{target_identifier}'. "
            f"Use the full agent ID to disambiguate.\n\n"
            f"To see all agent IDs, run:\n"
            f"  mngr list --fields id,name,host"
        )
    if len(matching_hosts) > 1:
        raise UserInputError(
            f"Multiple hosts found matching '{target_identifier}'. "
            f"Use the full host ID to disambiguate.\n\n"
            f"To see all IDs, run:\n"
            f"  mngr list --fields id,name,host"
        )

    has_agent_match = len(matching_agents) == 1
    has_host_match = len(matching_hosts) == 1

    # Check for cross-type ambiguity
    if has_agent_match and has_host_match:
        raise UserInputError(
            f"'{target_identifier}' matches both an agent and a host. "
            f"Use the full ID to disambiguate.\n\n"
            f"To see all IDs, run:\n"
            f"  mngr list --fields id,name,host"
        )

    # Neither matched
    if not has_agent_match and not has_host_match:
        raise UserInputError(
            f"No agent or host found matching: {target_identifier}\n\nTo see available agents, run:\n  mngr list"
        )

    # Agent matched
    if has_agent_match:
        discovered_host, discovered_agent = matching_agents[0]
        return _resolve_agent_target(
            discovered_host=discovered_host,
            discovered_agent=discovered_agent,
            mngr_ctx=mngr_ctx,
            relative_to=relative_to,
        )

    # Host matched
    discovered_host = matching_hosts[0]
    if relative_to != PathRelativeTo.HOST and relative_to != PathRelativeTo.WORK:
        raise UserInputError(
            f"--relative-to {relative_to.value.lower()} is only valid for agent targets. "
            f"Host targets always use MNGR_HOST_DIR as the base path."
        )
    return _resolve_host_target(
        discovered_host=discovered_host,
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
    except MngrError as err:
        logger.trace("Host {} is not available: {}", host_id, err)
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
    host_dir: Path | None = None
    if online_host is not None:
        host_dir = online_host.host_dir
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

    # Compute a synthetic host_dir for path computation when offline
    if host_dir is None:
        host_dir = Path("/mngr-host-dir")

    base_path = _compute_agent_base_path(
        relative_to=relative_to,
        work_dir=work_dir if work_dir is not None else Path("/unknown"),
        host_dir=host_dir,
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

    if online_host is not None:
        base_path = online_host.host_dir
    else:
        base_path = Path("/mngr-host-dir")

    logger.debug("Resolved host target: base_path={}, is_online={}", base_path, online_host is not None)

    return ResolveFileTargetResult(
        online_host=online_host,
        volume=volume,
        base_path=base_path,
        is_agent=False,
        agent_id=None,
        relative_to=PathRelativeTo.HOST,
    )
