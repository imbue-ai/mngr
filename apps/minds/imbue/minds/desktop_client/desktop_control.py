"""Install-scoped desktop control (provider config + host/state-container lifecycle).

Extracted from ``app.py`` so the cookie-only ``/api/v1/desktop/...`` routes (in
``api_v1.py``) can drive the same provider enable/disable, bulk host stop, and
Docker state-container stop the legacy UI routes drove, without importing
``app.py`` (which would be an import cycle). The functions take resolved
dependencies and raise typed errors the route maps to JSON; mirrors the
``workspace_lifecycle`` / ``workspace_create`` extraction pattern.
"""

import os
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.bootstrap import set_provider_is_enabled
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.mind_liveness import MindLiveness
from imbue.minds.desktop_client.mind_liveness import compute_mind_liveness_by_agent_id
from imbue.minds.desktop_client.mngr_command import run_mngr_to_completion
from imbue.minds.desktop_client.supertokens_routes import bounce_latchkey_forward_supervisor
from imbue.minds.envs.docker_cleanup import stop_active_env_state_container
from imbue.minds.errors import MngrCommandError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor


class ProviderHasActiveWorkspacesError(RuntimeError):
    """A provider cannot be disabled while it still has active workspaces.

    Carries the provider name and the active workspace ids so the route can
    surface a 409 with an explanatory JSON error.
    """

    def __init__(self, provider_name: str, active_agent_ids: Sequence[AgentId]) -> None:
        super().__init__(
            f"Provider '{provider_name}' has {len(active_agent_ids)} active workspace(s) and cannot be disabled."
        )
        self.provider_name = provider_name
        self.active_agent_ids = tuple(active_agent_ids)


def list_active_workspaces_for_provider(
    backend_resolver: BackendResolverInterface, provider_name: str
) -> list[AgentId]:
    """Return the active (non-DESTROYED-host) workspace agent ids served by ``provider_name``."""
    matching: list[AgentId] = []
    for agent_id in backend_resolver.list_active_workspace_ids():
        info = backend_resolver.get_agent_display_info(agent_id)
        if info is not None and info.provider_name == provider_name:
            matching.append(agent_id)
    return matching


def set_provider_enabled(
    provider_name: str,
    is_enabled: bool,
    backend_resolver: BackendResolverInterface,
    latchkey_forward_supervisor: LatchkeyForwardSupervisor | None,
) -> bool:
    """Idempotently set a provider's ``is_enabled`` flag; bounce observe when it changed.

    Refuses to disable a provider that still has active workspaces (raising
    :class:`ProviderHasActiveWorkspacesError`) -- disabling it would drop those
    live workspaces off discovery. Returns whether the settings file changed.
    """
    if not is_enabled:
        active = list_active_workspaces_for_provider(backend_resolver, provider_name)
        if active:
            raise ProviderHasActiveWorkspacesError(provider_name, active)
    changed = set_provider_is_enabled(provider_name, is_enabled)
    # Only bounce when the settings file actually changed -- a no-op toggle should
    # not trigger a SIGHUP and a full mngr observe restart.
    if changed:
        bounce_latchkey_forward_supervisor(latchkey_forward_supervisor)
    return changed


def running_workspace_entries(backend_resolver: BackendResolverInterface) -> list[dict[str, str]]:
    """Return ``[{id, name}, ...]`` for every shutdown-capable workspace currently RUNNING.

    Reads liveness from the in-memory discovery snapshot (plus any optimistic
    override) -- no subprocess -- so callers are instant.
    """
    running: list[dict[str, str]] = []
    for aid_str, state in compute_mind_liveness_by_agent_id(backend_resolver).items():
        if state != MindLiveness.RUNNING:
            continue
        aid = AgentId(aid_str)
        name = backend_resolver.get_workspace_name(aid)
        if not name:
            info = backend_resolver.get_agent_display_info(aid)
            name = info.agent_name if info is not None else aid_str
        running.append({"id": aid_str, "name": name})
    return running


def build_stop_hosts_argv(mngr_binary: str, agent_ids: Sequence[AgentId]) -> list[str]:
    """Build the argv for one variadic ``mngr stop <ids...> --quiet --stop-host``."""
    return [mngr_binary, "stop", *(str(aid) for aid in agent_ids), "--quiet", "--stop-host"]


def stop_workspace_hosts(
    requested_ids: Sequence[str],
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> list[dict[str, str]]:
    """Stop the hosts of the requested workspaces in one ``mngr stop --stop-host``.

    Each requested workspace agent is resolved to the system-services agent
    sharing its host (the host-stop target) and all are passed to a single
    synchronous ``mngr stop``. After the attempt, recomputes liveness and returns
    the requested workspaces still running (so the quit flow can offer Retry).
    """
    services_agent_ids: list[AgentId] = []
    host_ids: list[HostId] = []
    for agent_id in requested_ids:
        aid = AgentId(agent_id)
        services_agent_id = backend_resolver.get_system_services_agent_id(aid)
        if services_agent_id is None:
            logger.warning("Could not locate the system-services agent for host stop on {}", aid)
            continue
        services_agent_ids.append(services_agent_id)
        info = backend_resolver.get_agent_display_info(aid)
        if info is not None:
            try:
                host_ids.append(HostId(info.host_id))
            except ValueError:
                logger.warning("Could not resolve a host id for host stop on {}", aid)
    logger.info(
        "Quit-time bulk host stop: requested={} resolved services_agents={} host_ids={}",
        list(requested_ids),
        [str(a) for a in services_agent_ids],
        [str(h) for h in host_ids],
    )
    if services_agent_ids:
        env = dict(os.environ)
        env["MNGR_HOST_DIR"] = str(mngr_host_dir)
        argv = build_stop_hosts_argv(mngr_binary, services_agent_ids)
        try:
            run_mngr_to_completion(concurrency_group, argv, env)
        except MngrCommandError as exc:
            logger.warning("Bulk host stop failed for {}: {}", list(requested_ids), exc)
        else:
            logger.info("Quit-time bulk host stop succeeded; marking STOPPED: {}", [str(h) for h in host_ids])
            for host_id in host_ids:
                backend_resolver.set_host_state_override(host_id, HostState.STOPPED)
    requested_set = set(requested_ids)
    return [entry for entry in running_workspace_entries(backend_resolver) if entry["id"] in requested_set]


def stop_state_container(mngr_host_dir: Path, concurrency_group: ConcurrencyGroup) -> bool:
    """Stop this env's mngr Docker state container to fully free local resources at quit.

    Returns whether a stop was attempted (False for envs with no state container).
    Raises ``DockerCleanupError`` on a docker failure.
    """
    return stop_active_env_state_container(
        mngr_host_dir=mngr_host_dir,
        parent_concurrency_group=concurrency_group,
    )
