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
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.offline_host import try_resolve_readable_host
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
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


class ResolveFileTargetResult(FrozenModel):
    """Result of resolving a file command target to a readable host and base path."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    host: HostFileReadInterface = Field(description="Readable host (online host or readable stopped host)")
    base_path: Path = Field(description="Base path for resolving relative paths")
    is_agent: bool = Field(description="Whether the target is an agent (vs a host)")
    agent_id: AgentId | None = Field(default=None, description="Agent ID if target is an agent")
    relative_to: PathRelativeTo = Field(description="Path resolution mode")

    @property
    def is_online(self) -> bool:
        return isinstance(self.host, OnlineHostInterface)


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


def _get_readable_host(
    provider: BaseProviderInstance,
    host_id: HostId,
    target_display_name: str,
) -> HostFileReadInterface:
    """Return a readable host for ``host_id``, raising if no access is available.

    When the host is online, the live host is returned directly. When the host
    is stopped, a volume-backed readable host is returned (provided the provider
    surfaces a volume for the host); otherwise a clear error is raised.
    """
    host = try_resolve_readable_host(provider, host_id)
    if host is None:
        raise MngrError(
            f"{target_display_name} is offline and the provider does not support volume access. Cannot access files."
        )
    return host


def _host_dir_of(host: HostFileReadInterface) -> Path:
    """Return the ``host_dir`` of a readable host.

    Every readable host resolved for ``mngr file`` is also a
    :class:`HostInterface` (an online host or a volume-backed offline host), so
    it exposes a real ``host_dir``. This narrows the file-read interface to
    obtain it.
    """
    if not isinstance(host, HostInterface):
        raise MngrError(f"Readable host {host} does not expose a host_dir")
    return host.host_dir


def _resolve_agent_target(
    discovered_host: DiscoveredHost,
    discovered_agent: DiscoveredAgent,
    mngr_ctx: MngrContext,
    relative_to: PathRelativeTo,
) -> ResolveFileTargetResult:
    with log_span("Getting access for agent target"):
        provider = get_provider_instance(discovered_host.provider_name, mngr_ctx)

    host = _get_readable_host(
        provider=provider,
        host_id=discovered_host.host_id,
        target_display_name=f"Host for agent '{discovered_agent.agent_name}'",
    )
    is_online = isinstance(host, OnlineHostInterface)

    # Work-directory files live outside host_dir, so an offline volume-backed
    # host cannot read them. Keep an explicit, friendly error.
    if relative_to == PathRelativeTo.WORK and not is_online:
        raise UserInputError(
            "Host is offline. Work directory files are not accessible via volume. "
            "Use --relative-to state or --relative-to host for offline access."
        )

    # When online, get work_dir from the host's agent list.
    work_dir: Path | None = None
    if isinstance(host, OnlineHostInterface):
        for agent_ref in host.discover_agents():
            if agent_ref.agent_id == discovered_agent.agent_id:
                work_dir = agent_ref.work_dir
                break

    # Otherwise, fall back to the discovered work_dir.
    if work_dir is None:
        work_dir = discovered_agent.work_dir

    if work_dir is None and relative_to == PathRelativeTo.WORK:
        raise UserInputError(f"Could not determine work directory for agent: {discovered_agent.agent_name}")

    base_path = _compute_agent_base_path(
        relative_to=relative_to,
        work_dir=work_dir if work_dir is not None else Path("/unknown"),
        host_dir=_host_dir_of(host),
        agent_id=discovered_agent.agent_id,
    )
    logger.debug("Resolved agent target: base_path={}, is_online={}", base_path, is_online)

    return ResolveFileTargetResult(
        host=host,
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

    host = _get_readable_host(
        provider=provider,
        host_id=discovered_host.host_id,
        target_display_name=f"Host '{discovered_host.host_name}'",
    )

    base_path = _host_dir_of(host)
    logger.debug("Resolved host target: base_path={}, is_online={}", base_path, isinstance(host, OnlineHostInterface))

    return ResolveFileTargetResult(
        host=host,
        base_path=base_path,
        is_agent=False,
        agent_id=None,
        relative_to=PathRelativeTo.HOST,
    )
