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
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

# The catch-all detent permission (matches every request under the scope) is
# shown to users as "all", mirroring the permission-request dialog.
_WILDCARD_DISPLAY_LABEL = "all"
_WILDCARD_DESCRIPTION = "Unrestricted access: any request to this service is permitted."

# File-sharing *and* cross-workspace-management grants share the domain-only
# ``latchkey-self`` scope with baseline / accounts permissions (the gateway
# unions per-feature permission schemas onto it rather than minting dedicated
# scopes). So the whole ``latchkey-self`` rule must never be deleted; each
# feature is read back by its permission-name prefix and revoked by removing
# only its own permission names from the rule.
_SELF_SCOPE = "latchkey-self"

# File-sharing grants: per-path permission schemas named
# ``minds-file-server-<access>-<absolute-path>`` (see the gateway's
# ``permission_requests.mjs``).
_FILE_SHARING_PERMISSION_PREFIX = "minds-file-server-"
_FILE_SHARING_READ = "read"
_FILE_SHARING_WRITE = "write"
# User-facing labels: a write grant is a read+write superset, so it reads as
# "read and write"; a read-only grant reads as "read".
_FILE_SHARING_READ_LABEL = "read"
_FILE_SHARING_WRITE_LABEL = "read and write"

# Cross-workspace-management grants: verb permission schemas named
# ``minds-workspaces-<verb>`` (an all-workspaces grant) or
# ``minds-workspaces-<verb>-<target_agent_id>`` (a grant pinned to one target
# workspace). ``read`` / ``create`` are all-or-nothing; the rest are targeted.
_WORKSPACE_PERMISSION_PREFIX = "minds-workspaces-"
_WORKSPACE_VERB_BY_PERMISSION = {verb.permission: verb for verb in WORKSPACE_VERBS}
_TARGETED_WORKSPACE_VERB_PERMISSIONS = tuple(verb.permission for verb in WORKSPACE_VERBS if verb.is_targeted)


class PermissionOverviewError(Exception):
    """Raised for caller-facing programming errors (e.g. revoking an unknown service)."""


class GrantedPermission(FrozenModel):
    """A single granted permission plus its plain-English description for a tooltip."""

    label: str = Field(description="User-facing label (the detent catch-all ``any`` is rendered as ``all``).")
    description: str = Field(
        default="",
        description="Plain-English summary shown as a tooltip; empty when the catalog has none.",
    )


class SharedPath(FrozenModel):
    """A single shared filesystem path and the access level granted on it."""

    path: str = Field(description="Absolute path shared with the agent.")
    access_label: str = Field(description="User-facing access level: ``read`` or ``read and write``.")


class WorkspaceFileSharingGrant(FrozenModel):
    """The file-sharing access a single workspace's host has been granted.

    ``paths`` lists every shared path with its effective access level (a path that
    has a write grant reads as ``read and write``; read-only paths read as
    ``read``), sorted by path. The settings template renders these as full-width
    cards, one path per row, so the individual paths are visible rather than
    hidden behind a tooltip.
    """

    workspace_agent_id: str = Field(description="Primary workspace agent id (used to resolve the host on revoke).")
    workspace_name: str = Field(description="Human-readable workspace display name shown as the card header.")
    host_id: str = Field(description="Host the grant lives on (every agent on the host shares it).")
    color: str = Field(description="Workspace accent color hex (``#rrggbb``) for the card header dot.")
    paths: tuple[SharedPath, ...] = Field(description="Shared paths with their access level, sorted by path.")


class WorkspaceServiceGrant(FrozenModel):
    """The permissions a single workspace's host has been granted for one service."""

    workspace_agent_id: str = Field(description="Primary workspace agent id (used to resolve the host on revoke).")
    workspace_name: str = Field(description="Human-readable workspace display name shown as the column header.")
    host_id: str = Field(description="Host the grant lives on (every agent on the host shares it).")
    color: str = Field(description="Workspace accent color hex (``#rrggbb``) for the column header dot.")
    permissions: tuple[GrantedPermission, ...] = Field(
        description="Permissions granted under this service, in catalog order, each with its tooltip description.",
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


def _granted_permissions(
    service_infos: Sequence[ServicePermissionInfo],
    granted: frozenset[str],
) -> tuple[GrantedPermission, ...]:
    """Map the granted permission schemas to labelled, described permissions in catalog order.

    Iterates the catalog's declared permission schemas across every scope the
    service owns (``any`` is index 0 of each), keeping only those actually
    granted and de-duplicating across scopes. Grants that are not in the
    catalog for the service are dropped (defence-in-depth against a hand-edited
    file), and the catch-all ``any`` is relabeled ``all`` with a generic
    description.
    """
    permissions: list[GrantedPermission] = []
    seen: set[str] = set()
    for info in service_infos:
        for schema in info.permission_schemas:
            if schema not in granted or schema in seen:
                continue
            seen.add(schema)
            if schema == WILDCARD_PERMISSION_NAME:
                permissions.append(GrantedPermission(label=_WILDCARD_DISPLAY_LABEL, description=_WILDCARD_DESCRIPTION))
            else:
                permissions.append(
                    GrantedPermission(label=schema, description=info.description_by_permission_name.get(schema, ""))
                )
    return tuple(permissions)


def build_permission_overview(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
) -> tuple[ServicePermissionOverview, ...]:
    """Assemble the per-service, per-workspace grant overview for the settings page.

    Reads each active workspace host's permissions file once (through the
    gateway extension) and groups the grants by catalog service. Only services
    with at least one active-workspace grant are returned; the result is sorted
    by display name for a stable UI.

    Raises :class:`LatchkeyGatewayClientError` if a host file cannot be read.
    Because every host shares one gateway, a read error almost always means the
    gateway itself is unavailable, so the caller surfaces an explicit
    "unavailable" state rather than silently rendering the page as if nothing
    were granted (a missing file is not an error -- the client maps it to an
    empty rule set).
    """
    hosts = _list_active_workspace_hosts(backend_resolver)
    plugin_data_dir = latchkey.plugin_data_dir
    rules_by_agent: dict[str, dict[str, tuple[str, ...]]] = {}
    for host in hosts:
        path = permissions_path_for_host(plugin_data_dir, host.host_id)
        rules_by_agent[host.agent_id] = gateway_client.get_permission_rules(path)

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
            permissions = _granted_permissions(service_infos, frozenset(granted))
            if not permissions:
                continue
            grants.append(
                WorkspaceServiceGrant(
                    workspace_agent_id=host.agent_id,
                    workspace_name=host.workspace_name,
                    host_id=str(host.host_id),
                    color=host.color,
                    permissions=permissions,
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


def _parse_file_sharing_permission(permission_name: str) -> tuple[str, str] | None:
    """Split a ``minds-file-server-<access>-<path>`` name into ``(access, path)``.

    Returns ``None`` for any permission name that is not a well-formed
    file-sharing schema (so unrelated ``latchkey-self`` permissions -- baseline,
    accounts, workspace verbs -- are ignored). The access mode is the token
    before the first ``-`` after the prefix; the remainder (which starts with
    ``/``) is the absolute path.
    """
    if not permission_name.startswith(_FILE_SHARING_PERMISSION_PREFIX):
        return None
    remainder = permission_name[len(_FILE_SHARING_PERMISSION_PREFIX) :]
    access, separator, path = remainder.partition("-")
    if not separator or access not in (_FILE_SHARING_READ, _FILE_SHARING_WRITE) or not path:
        return None
    return access, path


def build_file_sharing_overview(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    latchkey: Latchkey,
) -> tuple[WorkspaceFileSharingGrant, ...]:
    """Assemble the per-workspace file-sharing grant overview for the settings page.

    Reads each active workspace host's permissions file once (through the gateway
    extension), pulls the ``minds-file-server-*`` permissions out of the shared
    ``latchkey-self`` rule, and lists every shared path with its effective access
    level (a path that has a write grant reads as ``read and write``). Only
    workspaces with at least one file-sharing grant are returned, sorted by
    workspace name. Raises :class:`LatchkeyGatewayClientError` on a read failure
    (see :func:`build_permission_overview`).
    """
    plugin_data_dir = latchkey.plugin_data_dir
    grants: list[WorkspaceFileSharingGrant] = []
    for host in _list_active_workspace_hosts(backend_resolver):
        path = permissions_path_for_host(plugin_data_dir, host.host_id)
        permissions = gateway_client.get_permission_rules(path).get(_SELF_SCOPE, ())
        read_paths: set[str] = set()
        write_paths: set[str] = set()
        for permission_name in permissions:
            parsed = _parse_file_sharing_permission(permission_name)
            if parsed is None:
                continue
            access, shared_path = parsed
            (write_paths if access == _FILE_SHARING_WRITE else read_paths).add(shared_path)
        all_paths = read_paths | write_paths
        if not all_paths:
            continue
        # A path with a write grant is read+write; otherwise read-only.
        shared_paths = tuple(
            SharedPath(
                path=shared_path,
                access_label=_FILE_SHARING_WRITE_LABEL if shared_path in write_paths else _FILE_SHARING_READ_LABEL,
            )
            for shared_path in sorted(all_paths)
        )
        grants.append(
            WorkspaceFileSharingGrant(
                workspace_agent_id=host.agent_id,
                workspace_name=host.workspace_name,
                host_id=str(host.host_id),
                color=host.color,
                paths=shared_paths,
            )
        )
    return tuple(sorted(grants, key=lambda grant: grant.workspace_name.lower()))


def _revoke_file_sharing_at_path(gateway_client: LatchkeyGatewayClient, permissions_file_path: Path) -> None:
    """Strip every ``minds-file-server-*`` permission from the host file's ``latchkey-self`` rule.

    The rule also carries unrelated baseline / accounts / workspace permissions,
    so we rewrite it with just the file-sharing entries filtered out rather than
    deleting the whole rule. A no-op when the host has no file-sharing grants.
    (The now-orphaned per-path schema definitions are left in the file's
    ``schemas`` object; they are unreferenced and harmless, and a re-grant
    overwrites them by name.)
    """
    permissions = gateway_client.get_permission_rules(permissions_file_path).get(_SELF_SCOPE, ())
    kept = tuple(name for name in permissions if _parse_file_sharing_permission(name) is None)
    if len(kept) == len(permissions):
        return
    gateway_client.set_permission_rule(permissions_file_path, _SELF_SCOPE, kept)


def revoke_file_sharing_for_workspace(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    latchkey: Latchkey,
    workspace_agent_id: str,
) -> None:
    """Remove all file-sharing grants from the given workspace's host file.

    Raises :class:`PermissionOverviewError` for an unresolvable workspace.
    """
    host_id = _resolve_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionOverviewError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot revoke.",
        )
    _revoke_file_sharing_at_path(gateway_client, permissions_path_for_host(latchkey.plugin_data_dir, host_id))


def revoke_file_sharing_for_all_workspaces(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    latchkey: Latchkey,
) -> int:
    """Remove all file-sharing grants from every active workspace host. Returns hosts processed."""
    plugin_data_dir = latchkey.plugin_data_dir
    hosts = _list_active_workspace_hosts(backend_resolver)
    for host in hosts:
        _revoke_file_sharing_at_path(gateway_client, permissions_path_for_host(plugin_data_dir, host.host_id))
    return len(hosts)


# -- Cross-workspace management ("workspace") grants ---------------------------


class WorkspaceOpGrantCard(FrozenModel):
    """One granting workspace's cross-workspace verbs within a single target group.

    ``workspace_agent_id`` / ``workspace_name`` identify the *granting* workspace
    (the agent that holds the permission); the target it acts on is carried by the
    enclosing :class:`WorkspaceOpTargetGroup`. ``permissions`` are the granted
    verb chips (short verb label + description tooltip), in catalog order.
    """

    workspace_agent_id: str = Field(description="Granting workspace agent id (used to resolve the host on revoke).")
    workspace_name: str = Field(description="Granting workspace display name shown as the card header.")
    host_id: str = Field(description="Host the grant lives on.")
    color: str = Field(description="Granting workspace accent color hex (``#rrggbb``).")
    permissions: tuple[GrantedPermission, ...] = Field(description="Granted verb chips, in catalog order.")


class WorkspaceOpTargetGroup(FrozenModel):
    """All granting workspaces that hold cross-workspace verbs for a single target.

    A group is either the *shared* group (verbs that apply to every workspace --
    ``read`` / ``create`` and any all-workspaces grant of a targeted verb;
    ``is_shared`` True, ``target_workspace_id`` empty) or a per-target group
    (verbs pinned to one target workspace; ``is_shared`` False).
    """

    target_workspace_id: str = Field(description="Target workspace agent id, or empty string for the shared group.")
    target_name: str = Field(description="Human-readable target label (``all workspaces`` or the target's name).")
    is_shared: bool = Field(description="Whether this is the all-workspaces group.")
    cards: tuple[WorkspaceOpGrantCard, ...] = Field(description="One card per granting workspace in this group.")


def _parse_workspace_permission(permission_name: str) -> tuple[str, str | None] | None:
    """Split a ``minds-workspaces-*`` permission into ``(verb_permission, target)``.

    ``target`` is ``None`` for an all-workspaces grant (a broad verb name) and the
    target workspace agent id for a per-target grant. Returns ``None`` for any name
    that is not a well-formed workspace verb, so unrelated ``latchkey-self``
    permissions (baseline / accounts / file-sharing) are ignored. Matching is by
    the known verb names (not naive ``-`` splitting) because verb names such as
    ``minds-workspaces-backups-export`` themselves contain hyphens.
    """
    if not permission_name.startswith(_WORKSPACE_PERMISSION_PREFIX):
        return None
    if permission_name in _WORKSPACE_VERB_BY_PERMISSION:
        return permission_name, None
    for verb_permission in _TARGETED_WORKSPACE_VERB_PERMISSIONS:
        prefix = f"{verb_permission}-"
        if permission_name.startswith(prefix):
            target = permission_name[len(prefix) :]
            if target:
                return verb_permission, target
    return None


def _workspace_verb_chips(verb_permissions: set[str]) -> tuple[GrantedPermission, ...]:
    """Build verb chips (short label + description tooltip) in catalog order."""
    chips: list[GrantedPermission] = []
    for verb in WORKSPACE_VERBS:
        if verb.permission in verb_permissions:
            chips.append(
                GrantedPermission(
                    label=verb.permission.removeprefix(_WORKSPACE_PERMISSION_PREFIX),
                    description=verb.description,
                )
            )
    return tuple(chips)


def _resolve_target_workspace_name(backend_resolver: BackendResolverInterface, target_workspace_id: str) -> str:
    """Resolve a target workspace agent id to a display name, falling back to the raw id."""
    try:
        parsed = AgentId(target_workspace_id)
    except ValueError:
        return target_workspace_id
    name = backend_resolver.get_workspace_name(parsed)
    if name:
        return name
    info = backend_resolver.get_agent_display_info(parsed)
    return info.agent_name if info is not None else target_workspace_id


def _build_workspace_op_group(
    target: str | None,
    per_agent: dict[str, set[str]],
    host_by_agent: dict[str, _WorkspaceHost],
    backend_resolver: BackendResolverInterface,
) -> WorkspaceOpTargetGroup:
    """Assemble one target group's cards from its granting-agent -> verbs mapping.

    ``target`` is ``None`` for the shared (all-workspaces) group; otherwise the
    target workspace agent id. Cards are sorted by granting-workspace name.
    """
    cards = [
        WorkspaceOpGrantCard(
            workspace_agent_id=host_by_agent[agent_id].agent_id,
            workspace_name=host_by_agent[agent_id].workspace_name,
            host_id=str(host_by_agent[agent_id].host_id),
            color=host_by_agent[agent_id].color,
            permissions=_workspace_verb_chips(verb_permissions),
        )
        for agent_id, verb_permissions in per_agent.items()
    ]
    cards.sort(key=lambda card: card.workspace_name.lower())
    return WorkspaceOpTargetGroup(
        target_workspace_id="" if target is None else target,
        target_name="all workspaces" if target is None else _resolve_target_workspace_name(backend_resolver, target),
        is_shared=target is None,
        cards=tuple(cards),
    )


def _workspace_permission_targets(permission_name: str, target_workspace_id: str | None) -> bool:
    """Whether ``permission_name`` is a workspace verb scoped to ``target_workspace_id``.

    ``target_workspace_id`` is ``None`` for the shared (all-workspaces) grants.
    Non-workspace permissions never match.
    """
    parsed = _parse_workspace_permission(permission_name)
    return parsed is not None and parsed[1] == target_workspace_id


def build_workspace_overview(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    latchkey: Latchkey,
) -> tuple[WorkspaceOpTargetGroup, ...]:
    """Assemble the cross-workspace-management grant overview, grouped by target.

    Reads each active workspace host's permissions file once, pulls the
    ``minds-workspaces-*`` verbs out of the shared ``latchkey-self`` rule, and
    groups them first by *target* (the shared/all-workspaces bucket, then one
    bucket per specific target workspace) and within each target by *granting*
    workspace. Only targets with at least one grant are returned; the shared
    group is listed first, then per-target groups sorted by target name. Raises
    :class:`LatchkeyGatewayClientError` on a read failure (see
    :func:`build_permission_overview`).
    """
    plugin_data_dir = latchkey.plugin_data_dir
    hosts = _list_active_workspace_hosts(backend_resolver)
    # target key (None == shared) -> granting agent id -> set of verb permissions.
    verbs_by_target: dict[str | None, dict[str, set[str]]] = {}
    host_by_agent: dict[str, _WorkspaceHost] = {}
    for host in hosts:
        host_by_agent[host.agent_id] = host
        permissions = gateway_client.get_permission_rules(
            permissions_path_for_host(plugin_data_dir, host.host_id)
        ).get(_SELF_SCOPE, ())
        for permission_name in permissions:
            parsed = _parse_workspace_permission(permission_name)
            if parsed is None:
                continue
            verb_permission, target = parsed
            verbs_by_target.setdefault(target, {}).setdefault(host.agent_id, set()).add(verb_permission)

    groups: list[WorkspaceOpTargetGroup] = []
    if None in verbs_by_target:
        groups.append(_build_workspace_op_group(None, verbs_by_target[None], host_by_agent, backend_resolver))
    target_groups = [
        _build_workspace_op_group(target, per_agent, host_by_agent, backend_resolver)
        for target, per_agent in verbs_by_target.items()
        if target is not None
    ]
    target_groups.sort(key=lambda group: group.target_name.lower())
    groups.extend(target_groups)
    return tuple(groups)


def _revoke_workspace_ops_at_path(
    gateway_client: LatchkeyGatewayClient,
    permissions_file_path: Path,
    target_workspace_id: str | None,
) -> None:
    """Remove the ``minds-workspaces-*`` permissions for one target scope from the host file.

    ``target_workspace_id`` is ``None`` for the shared (all-workspaces) grants or a
    target agent id for a per-target group. Only the matching workspace verbs are
    stripped from the ``latchkey-self`` rule; unrelated permissions are preserved.
    """
    permissions = gateway_client.get_permission_rules(permissions_file_path).get(_SELF_SCOPE, ())
    kept = tuple(name for name in permissions if not _workspace_permission_targets(name, target_workspace_id))
    if len(kept) == len(permissions):
        return
    gateway_client.set_permission_rule(permissions_file_path, _SELF_SCOPE, kept)


def revoke_workspace_ops_for_workspace(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    latchkey: Latchkey,
    workspace_agent_id: str,
    target_workspace_id: str | None,
) -> None:
    """Remove one granting workspace's cross-workspace verbs for a single target scope.

    ``target_workspace_id`` is ``None`` for the shared group. Raises
    :class:`PermissionOverviewError` for an unresolvable granting workspace.
    """
    host_id = _resolve_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionOverviewError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot revoke.",
        )
    _revoke_workspace_ops_at_path(
        gateway_client, permissions_path_for_host(latchkey.plugin_data_dir, host_id), target_workspace_id
    )


def revoke_workspace_ops_for_all_workspaces(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    latchkey: Latchkey,
    target_workspace_id: str | None,
) -> int:
    """Remove a target scope's cross-workspace verbs from every active workspace host.

    ``target_workspace_id`` is ``None`` for the shared group. Returns hosts processed.
    """
    plugin_data_dir = latchkey.plugin_data_dir
    hosts = _list_active_workspace_hosts(backend_resolver)
    for host in hosts:
        _revoke_workspace_ops_at_path(
            gateway_client, permissions_path_for_host(plugin_data_dir, host.host_id), target_workspace_id
        )
    return len(hosts)


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
