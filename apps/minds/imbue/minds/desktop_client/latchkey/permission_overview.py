"""Cross-workspace read/revoke view of predefined (catalog-backed) latchkey grants.

Backs the App-level settings "Permissions" section: it enumerates the
predefined-service permissions granted on every *active* workspace's host and
lets the user revoke them. Revocation removes the rule from that host's
``latchkey_permissions.json`` (through the gateway's bundled ``permissions``
extension, the single owner of on-disk permission writes); stored credentials
are left untouched, so a fresh grant does not force the user to re-authenticate.

Permissions are stored per host -- every agent on a host shares one
``latchkey_permissions.json`` (see :func:`permissions_path_for_host`). Minds
workspaces map 1:1 to hosts, so each column in the settings view is one
workspace, labelled by its primary agent's display name. Only non-destroyed
workspaces are shown (via
:meth:`BackendResolverInterface.list_active_workspace_ids`).

This module is deliberately read/revoke only: changing (broadening or
narrowing) an existing grant is done through the ordinary agent-driven
permission-request flow, not here.
"""

from collections.abc import Sequence

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import permissions_path_for_host

# The catch-all detent permission (matches every request under the scope) is
# shown to users as "all", mirroring the permission-request dialog.
_WILDCARD_DISPLAY_LABEL = "all"


class PermissionOverviewError(Exception):
    """Raised for caller-facing programming errors (e.g. revoking an unknown service)."""


class WorkspaceServiceGrant(FrozenModel):
    """The permissions a single workspace's host has been granted for one service."""

    workspace_agent_id: str = Field(description="Primary workspace agent id (used to resolve the host on revoke).")
    workspace_name: str = Field(description="Human-readable workspace display name shown as the column header.")
    host_id: str = Field(description="Host the grant lives on (every agent on the host shares it).")
    color: str = Field(description="Workspace accent color hex (``#rrggbb``) for the column header dot.")
    permission_labels: tuple[str, ...] = Field(
        description=(
            "User-facing permission labels granted under this service, in catalog order. The "
            "detent catch-all ``any`` is rendered as ``all``."
        ),
    )


class ServicePermissionOverview(FrozenModel):
    """All active-workspace grants for a single predefined service."""

    service_name: str = Field(description="Raw service name (e.g. ``slack``); used as the revoke action key.")
    display_name: str = Field(description="Human-readable service label shown as the section header.")
    workspace_grants: tuple[WorkspaceServiceGrant, ...] = Field(
        description="One entry per active workspace that has at least one permission for this service.",
    )


class _WorkspaceHost(FrozenModel):
    """An active workspace resolved to its host and display metadata."""

    agent_id: str
    workspace_name: str
    host_id: HostId
    color: str


def _list_active_workspace_hosts(backend_resolver: BackendResolverInterface) -> tuple[_WorkspaceHost, ...]:
    """Resolve every active (non-destroyed) workspace to its host + display metadata.

    Skips workspaces whose host cannot be resolved yet (transient discovery gap)
    or whose resolver reports a non-:class:`HostId` placeholder (e.g. the static
    resolver's ``"localhost"``). De-duplicates by host so a host that somehow
    carries two primary agents is only listed once (first wins).
    """
    hosts: list[_WorkspaceHost] = []
    seen_host_ids: set[HostId] = set()
    for agent_id in backend_resolver.list_active_workspace_ids():
        info = backend_resolver.get_agent_display_info(agent_id)
        if info is None:
            continue
        try:
            host_id = HostId(info.host_id)
        except ValueError:
            logger.debug("Skipping workspace {} with non-HostId host {!r}", agent_id, info.host_id)
            continue
        if host_id in seen_host_ids:
            continue
        seen_host_ids.add(host_id)
        workspace_name = backend_resolver.get_workspace_name(agent_id) or info.agent_name
        color = backend_resolver.get_workspace_color(agent_id) or DEFAULT_WORKSPACE_COLOR
        hosts.append(
            _WorkspaceHost(
                agent_id=str(agent_id),
                workspace_name=workspace_name,
                host_id=host_id,
                color=color,
            )
        )
    return tuple(hosts)


def _permission_labels(
    service_infos: Sequence[ServicePermissionInfo],
    granted: frozenset[str],
) -> tuple[str, ...]:
    """Map the granted permission schemas to user-facing labels in catalog order.

    Iterates the catalog's declared permission schemas across every scope the
    service owns (``any`` is index 0 of each), keeping only those actually
    granted and de-duplicating across scopes. Grants that are not in the
    catalog for the service are dropped (defence-in-depth against a hand-edited
    file), and the catch-all ``any`` is relabeled ``all``.
    """
    labels: list[str] = []
    seen: set[str] = set()
    for info in service_infos:
        for schema in info.permission_schemas:
            if schema in granted and schema not in seen:
                seen.add(schema)
                labels.append(_WILDCARD_DISPLAY_LABEL if schema == WILDCARD_PERMISSION_NAME else schema)
    return tuple(labels)


def build_permission_overview(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
) -> tuple[ServicePermissionOverview, ...]:
    """Assemble the per-service, per-workspace grant overview for the settings page.

    Reads each active workspace host's permissions file once (through the
    gateway extension) and groups the grants by catalog service. A host whose
    file cannot be read is logged and treated as having no grants rather than
    failing the whole page. Only services with at least one active-workspace
    grant are returned; the result is sorted by display name for a stable UI.
    """
    hosts = _list_active_workspace_hosts(backend_resolver)
    plugin_data_dir = latchkey.plugin_data_dir
    rules_by_agent: dict[str, dict[str, tuple[str, ...]]] = {}
    for host in hosts:
        path = permissions_path_for_host(plugin_data_dir, host.host_id)
        try:
            rules_by_agent[host.agent_id] = gateway_client.get_permission_rules(path)
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not read permissions for host {} via the gateway extension; treating as no grants: {}",
                host.host_id,
                e,
            )
            rules_by_agent[host.agent_id] = {}

    overviews: list[ServicePermissionOverview] = []
    for service_name, service_infos in services_catalog.as_mapping().items():
        if not service_infos:
            continue
        scopes = tuple(info.scope for info in service_infos)
        grants: list[WorkspaceServiceGrant] = []
        for host in hosts:
            rules = rules_by_agent[host.agent_id]
            granted: set[str] = set()
            for scope in scopes:
                granted.update(rules.get(scope, ()))
            labels = _permission_labels(service_infos, frozenset(granted))
            if not labels:
                continue
            grants.append(
                WorkspaceServiceGrant(
                    workspace_agent_id=host.agent_id,
                    workspace_name=host.workspace_name,
                    host_id=str(host.host_id),
                    color=host.color,
                    permission_labels=labels,
                )
            )
        if grants:
            overviews.append(
                ServicePermissionOverview(
                    service_name=service_name,
                    display_name=service_infos[0].display_name,
                    workspace_grants=tuple(grants),
                )
            )
    return tuple(sorted(overviews, key=lambda overview: overview.display_name.lower()))


def _resolve_host_id(
    backend_resolver: BackendResolverInterface,
    workspace_agent_id: str,
) -> HostId | None:
    """Resolve a workspace agent id to its :class:`HostId`, or ``None`` if unknown."""
    try:
        parsed = AgentId(workspace_agent_id)
    except ValueError:
        return None
    info = backend_resolver.get_agent_display_info(parsed)
    if info is None:
        return None
    try:
        return HostId(info.host_id)
    except ValueError:
        return None


def revoke_service_for_workspace(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    workspace_agent_id: str,
    service_name: str,
) -> None:
    """Remove every rule for ``service_name`` from the given workspace's host file.

    A service may own more than one detent scope; each is deleted. Raises
    :class:`PermissionOverviewError` for an unknown service or an
    unresolvable workspace (the caller maps these to a 400 / 503).
    """
    service_infos = services_catalog.get(service_name)
    if not service_infos:
        raise PermissionOverviewError(f"Unknown service '{service_name}'.")
    host_id = _resolve_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionOverviewError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot revoke.",
        )
    path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    for info in service_infos:
        gateway_client.delete_permission_rule(path, info.scope)


def revoke_service_for_all_workspaces(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    service_name: str,
) -> int:
    """Remove every rule for ``service_name`` from every active workspace host.

    Returns the number of workspace hosts processed. Raises
    :class:`PermissionOverviewError` for an unknown service.
    """
    service_infos = services_catalog.get(service_name)
    if not service_infos:
        raise PermissionOverviewError(f"Unknown service '{service_name}'.")
    plugin_data_dir = latchkey.plugin_data_dir
    hosts = _list_active_workspace_hosts(backend_resolver)
    for host in hosts:
        path = permissions_path_for_host(plugin_data_dir, host.host_id)
        for info in service_infos:
            gateway_client.delete_permission_rule(path, info.scope)
    return len(hosts)
