"""Shared workspace-metadata mutation (color + account association) for the desktop client.

Extracted from ``app.py`` so the agent-facing ``PATCH /api/v1/workspaces/<id>``
route (in ``api_v1.py``) can apply the same color-label write and account
associate/disassociate the browser settings page applies, without importing
``app.py`` (which would be an import cycle). The functions take resolved
dependencies and raise typed errors carrying the HTTP status the route surfaces
as JSON; mirrors the ``workspace_lifecycle`` / ``workspace_create`` extraction
pattern.
"""

import json
import os
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.api_models import WorkspaceResizeDimension
from imbue.minds.desktop_client.api_models import WorkspaceResizeResponse
from imbue.minds.desktop_client.api_models import WorkspaceResourceValues
from imbue.minds.desktop_client.api_models import WorkspaceResourcesResponse
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.mngr_command import run_mngr_to_completion
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.tunnel_token_injection import clear_tunnel_token_from_agent
from imbue.minds.desktop_client.workspace_color import normalize_workspace_color
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import WorkspaceSyncError
from imbue.mngr.primitives import AgentId

# Leased imbue_cloud hosts surface under a per-account provider instance named
# ``imbue_cloud_<account-slug>``; the trailing-underscore prefix matches those
# while excluding the hidden bare ``imbue_cloud`` singleton (which never hosts a
# user workspace). Mirrors ``app.py``'s ``_IMBUE_CLOUD_PROVIDER_PREFIX``.
_IMBUE_CLOUD_PROVIDER_PREFIX: Final[str] = "imbue_cloud_"


class WorkspaceColorError(RuntimeError):
    """A color write failed; ``code`` is the JSON discriminant, ``status_code`` the HTTP status.

    The route maps this to ``{"error": <code>}`` with ``status_code``. Codes:
    ``invalid_hex`` (400), ``not_primary`` (404), ``stale_provider`` (409),
    ``host_unreachable`` (502).
    """

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class WorkspaceAssociationError(RuntimeError):
    """An account associate/disassociate was rejected; carries the HTTP status to surface."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class WorkspaceResizeError(RuntimeError):
    """A resource read or resize failed; carries the HTTP status to surface."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _is_workspace_provider_errored(info: AgentDisplayInfo | None, errored_provider_names: set[str]) -> bool:
    """True when the agent's provider's most recent discovery poll errored (so the host is stale)."""
    return info is not None and info.provider_name is not None and info.provider_name in errored_provider_names


def _is_leased_imbue_cloud_workspace(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> bool:
    """Return True if the workspace runs on a host leased from imbue_cloud (per-account provider)."""
    info = backend_resolver.get_agent_display_info(agent_id)
    if info is None or info.provider_name is None:
        return False
    return info.provider_name.startswith(_IMBUE_CLOUD_PROVIDER_PREFIX)


def set_workspace_color(
    agent_id: AgentId,
    raw_hex: str,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup | None,
) -> str:
    """Validate + write the per-workspace ``color`` label; return the normalized ``#rrggbb`` hex.

    Writes via ``mngr label`` (CLI merge semantics, so other labels are
    preserved) and optimistically updates the resolver snapshot so the next SSE
    workspaces tick reflects the new color. Raises :class:`WorkspaceColorError`
    for every failure mode.
    """
    normalized = normalize_workspace_color(raw_hex)
    if normalized is None:
        raise WorkspaceColorError("invalid_hex", 400)
    # Color writes only apply to primary workspace agents (the ``workspace`` +
    # ``is_primary`` label pair); the sibling system-services agent shares the
    # host but does not own workspace identity.
    if agent_id not in backend_resolver.list_known_workspace_ids():
        raise WorkspaceColorError("not_primary", 404)
    info = backend_resolver.get_agent_display_info(agent_id)
    errored_provider_names = {str(name) for name in backend_resolver.get_provider_errors()}
    if _is_workspace_provider_errored(info, errored_provider_names):
        raise WorkspaceColorError("stale_provider", 409)
    if concurrency_group is None:
        logger.warning("No concurrency group available; cannot write color label for {}", agent_id)
        raise WorkspaceColorError("host_unreachable", 502)

    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    argv = [mngr_binary, "label", str(agent_id), "-l", f"color={normalized}"]
    try:
        run_mngr_to_completion(concurrency_group, argv, env)
    except MngrCommandError as exc:
        logger.warning("mngr label failed for {}: {}", agent_id, exc)
        raise WorkspaceColorError("host_unreachable", 502) from exc

    if isinstance(backend_resolver, MngrCliBackendResolver):
        backend_resolver.set_workspace_color_locally(agent_id, normalized)
    return normalized


def _run_mngr_limit_json(
    argv_tail: list[str],
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> dict[str, Any]:
    """Run ``mngr limit <argv_tail> --format json`` and return the parsed JSON object."""
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    argv = [mngr_binary, "limit", *argv_tail, "--format", "json"]
    try:
        stdout = run_mngr_to_completion(concurrency_group, argv, env)
    except MngrCommandError as exc:
        logger.warning("mngr limit failed ({}): {}", argv_tail, exc)
        raise WorkspaceResizeError(f"mngr limit failed: {exc}", 502) from exc
    try:
        return json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        logger.warning("mngr limit produced unparseable output: {}", stdout[:200])
        raise WorkspaceResizeError("mngr limit produced unparseable output", 502) from exc


def _resource_values_from_entry(entry: dict[str, Any] | None) -> WorkspaceResourceValues | None:
    if entry is None:
        return None
    return WorkspaceResourceValues(cpu_count=entry.get("cpu_count"), memory_gib=entry.get("memory_gib"))


def _resize_dimension_from_entry(entry: dict[str, Any] | None) -> WorkspaceResizeDimension | None:
    if entry is None:
        return None
    return WorkspaceResizeDimension(
        minimum=entry.get("minimum", 1),
        default_value=entry.get("default_value"),
        ceiling=entry.get("ceiling"),
    )


def get_workspace_resources(
    agent_id: AgentId,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> WorkspaceResourcesResponse:
    """Read the workspace host's resize capabilities plus configured and actual resource values.

    Shells out to ``mngr limit``'s read mode. Raises :class:`WorkspaceResizeError`
    when the command fails or reports nothing for the agent.
    """
    parsed = _run_mngr_limit_json([str(agent_id)], mngr_binary, mngr_host_dir, concurrency_group)
    host_entries = parsed.get("hosts") or []
    if not host_entries:
        raise WorkspaceResizeError("mngr limit reported no host for the workspace", 502)
    entry = host_entries[0]
    capabilities = entry.get("capabilities") or {}
    return WorkspaceResourcesResponse(
        agent_id=str(agent_id),
        supported=bool(capabilities.get("is_resize_supported")),
        cpu=_resize_dimension_from_entry(capabilities.get("cpu")),
        memory_gib=_resize_dimension_from_entry(capabilities.get("memory_gib")),
        configured=_resource_values_from_entry(entry.get("configured")),
        actual=_resource_values_from_entry(entry.get("actual")),
    )


def resize_workspace_resources(
    agent_id: AgentId,
    cpus: int | str | None,
    memory_gib: int | str | None,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> WorkspaceResizeResponse:
    """Set the workspace host's CPU/memory allotment via ``mngr limit`` (set-only; never restarts).

    ``cpus``/``memory_gib`` are positive integers or the literal ``'default'``.
    Returns the persisted configured values and the probed actual values; a
    discrepancy means the change applies on the host's next restart. Raises
    :class:`WorkspaceResizeError` on failure.
    """
    argv_tail = [str(agent_id)]
    if cpus is not None:
        argv_tail.extend(["--cpus", str(cpus)])
    if memory_gib is not None:
        argv_tail.extend(["--memory", str(memory_gib)])
    parsed = _run_mngr_limit_json(argv_tail, mngr_binary, mngr_host_dir, concurrency_group)
    resource_change = next(
        (change for change in parsed.get("changes") or [] if change.get("type") == "host_resources"),
        None,
    )
    if resource_change is None:
        raise WorkspaceResizeError("mngr limit reported no resource change for the workspace", 502)
    configured = _resource_values_from_entry(resource_change.get("configured"))
    if configured is None:
        raise WorkspaceResizeError("mngr limit reported no configured values for the workspace", 502)
    return WorkspaceResizeResponse(
        agent_id=str(agent_id),
        configured=configured,
        actual=_resource_values_from_entry(resource_change.get("actual")),
    )


def associate_workspace_account(
    agent_id: AgentId,
    account_id: str,
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
) -> AccountSession:
    """Bind ``agent_id`` to a signed-in account by creating its workspace record; wake the chrome SSE.

    ``account_id`` may be either the account's user id *or* its email -- it is
    resolved against the currently signed-in accounts, and the canonical
    :class:`AccountSession` (carrying the resolved ``user_id`` + ``email``) is
    returned so the caller can echo what was actually bound.

    Raises :class:`WorkspaceAssociationError`: 403 for a leased imbue_cloud host
    (permanently bound to its leasing account), 409 when no session store is
    configured, 404 when no signed-in account matches the given id/email, or
    502 when the record push fails (association requires connectivity -- the
    record is the association, and it lives on the connector).
    """
    if _is_leased_imbue_cloud_workspace(backend_resolver, agent_id):
        raise WorkspaceAssociationError("Cannot change the account association of a host leased from imbue_cloud", 403)
    if session_store is None:
        raise WorkspaceAssociationError("Session store is not configured", 409)
    matched = next(
        (account for account in session_store.list_accounts() if account_id in (account.user_id, account.email)),
        None,
    )
    if matched is None:
        raise WorkspaceAssociationError(
            f"No signed-in account matches {account_id!r}; pass the id or email of a signed-in account.",
            404,
        )
    try:
        session_store.associate_workspace(matched.user_id, str(agent_id), backend_resolver)
    except WorkspaceSyncError as exc:
        raise WorkspaceAssociationError(f"Could not associate the workspace: {exc}", 502) from exc
    # Wake the chrome SSE so the tile picks up its new 'account' field
    # immediately rather than at the next discovery heartbeat.
    if isinstance(backend_resolver, MngrCliBackendResolver):
        backend_resolver.notify_change()
    return matched


def disassociate_workspace_account(
    agent_id: AgentId,
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
    imbue_cloud_cli: ImbueCloudCli | None,
) -> None:
    """Unbind ``agent_id`` from its account and tear down its Cloudflare tunnel.

    A no-op if no session store is configured or the workspace has no associated
    account. Raises :class:`WorkspaceAssociationError`: 403 for a leased
    imbue_cloud host, 502 when the record removal fails (disassociation
    requires connectivity).
    """
    if _is_leased_imbue_cloud_workspace(backend_resolver, agent_id):
        raise WorkspaceAssociationError("Cannot disassociate a host leased from imbue_cloud", 403)
    if session_store is None:
        return
    account = session_store.get_account_for_workspace(str(agent_id))
    if account is None:
        return
    # Tear down the Cloudflare tunnel for this agent (if any). The plugin owns
    # tunnel state; after deleting it server-side, also clear the token file
    # inside the agent so its cloudflare-tunnel service stops cloudflared.
    if imbue_cloud_cli is not None:
        try:
            tunnel = imbue_cloud_cli.find_tunnel_for_agent(account=str(account.email), agent_id=str(agent_id))
            if tunnel is not None:
                imbue_cloud_cli.delete_tunnel(account=str(account.email), tunnel_name=tunnel.tunnel_name)
                clear_tunnel_token_from_agent(agent_id, imbue_cloud_cli.mngr_caller)
        except ImbueCloudCliError as exc:
            logger.warning("Failed to delete tunnel during disassociation: {}", exc)
    try:
        session_store.disassociate_workspace(str(account.user_id), str(agent_id))
    except WorkspaceSyncError as exc:
        raise WorkspaceAssociationError(f"Could not disassociate the workspace: {exc}", 502) from exc
    if isinstance(backend_resolver, MngrCliBackendResolver):
        backend_resolver.notify_change()
