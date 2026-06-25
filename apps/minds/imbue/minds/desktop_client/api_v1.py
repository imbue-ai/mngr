"""REST API v1 blueprint for the minds desktop client.

Every route under ``/api/v1/`` requires ``Authorization: Bearer <key>``
where ``<key>`` is the central :mod:`api_key_store` minds API key. The
latchkey gateway's bundled ``minds-api-proxy`` extension injects that
header on every forwarded request, so a caller (an agent in a
workspace) reaches us by hitting ``$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/...``.

Agent identity, when a route needs it, comes from the URL path's
``<agent_id>`` parameter -- *not* from the bearer token. The gateway's
per-host permissions file is what gates which agent ids a given caller
can talk about: at agent creation time the desktop client narrows the
host's permission rule to ``/minds-api-proxy/api/v1/agents/<agent_id>/...``,
so a request that reaches a route with a given ``<agent_id>`` has
already been authorized by the gateway as "this is an agent that lives
on the caller's host".
"""

import json

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import backup_status
from imbue.minds.desktop_client import workspace_version
from imbue.minds.desktop_client.api_key_auth import require_minds_api_key
from imbue.minds.desktop_client.backup_export import BackupExportError
from imbue.minds.desktop_client.backup_export import export_latest_snapshot_zip
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.responses import make_file_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return make_response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _json_error(message: str, status_code: int) -> Response:
    return _json_response({"error": message}, status_code=status_code)


# -- Notification route --


@require_minds_api_key
def _handle_notification(agent_id: str) -> Response:
    """Send a notification on behalf of the named agent."""
    dispatcher: NotificationDispatcher | None = get_state().notification_dispatcher
    if dispatcher is None:
        return _json_error("Notification dispatch not configured", 501)

    # force=True parses the body regardless of Content-Type, matching the old
    # FastAPI ``await request.json()`` (which ignored the header).
    body = request.get_json(silent=True, force=True)
    if body is None:
        return _json_error("Invalid JSON body", 400)
    if not isinstance(body, dict):
        return _json_error("Request body must be a JSON object", 400)

    message = body.get("message")
    if not message or not isinstance(message, str):
        return _json_error("'message' field is required and must be a string", 400)

    title = body.get("title")
    if title is not None and not isinstance(title, str):
        return _json_error("'title' field must be a string", 400)
    urgency_str = body.get("urgency", "NORMAL")
    try:
        urgency = NotificationUrgency(urgency_str.upper())
    except (ValueError, AttributeError):
        return _json_error(f"Invalid urgency: {urgency_str}. Must be one of: low, normal, critical", 400)

    parsed_agent_id = AgentId(agent_id)
    notification_request = NotificationRequest(
        message=message,
        title=title,
        urgency=urgency,
    )

    agent_info = get_state().backend_resolver.get_agent_display_info(parsed_agent_id)
    agent_display_name = agent_info.agent_name if agent_info else str(parsed_agent_id)

    dispatcher.dispatch(notification_request, agent_display_name)
    return _json_response({"ok": True})


# -- Cross-workspace management routes --
#
# These let an agent in one workspace act on *other* workspaces (and their
# backups) through the hub. Every route is gated at the gateway by the
# ``minds-workspaces`` detent scope (see ``mngr_latchkey.agent_setup``); the
# scope's per-verb permissions decide which of these a given caller may reach.
# A workspace is addressed by its primary (``is_primary``+``workspace``) agent
# id, matching minds discovery.


def _serialize_workspace(agent_id: AgentId) -> dict[str, object]:
    """Build the JSON summary for one workspace from discovery + its labels."""
    backend_resolver = get_state().backend_resolver
    info = backend_resolver.get_agent_display_info(agent_id)
    host_id = info.host_id if info is not None else None
    # ``host_id`` is the real ``host-<hex>`` id from discovery; static / in-memory
    # resolvers (and tests) report the placeholder ``"localhost"`` which is not a
    # valid HostId, so guard the lookup and treat the state as unknown there.
    host_state = None
    if host_id is not None:
        try:
            typed_host_id = HostId(host_id)
        except ValueError:
            typed_host_id = None
        if typed_host_id is not None:
            host_state = backend_resolver.get_host_state(typed_host_id)
    return {
        "agent_id": str(agent_id),
        "name": backend_resolver.get_workspace_name(agent_id) or (info.agent_name if info is not None else None),
        "host_id": host_id,
        "host_state": str(host_state) if host_state is not None else None,
        "provider_name": info.provider_name if info is not None else None,
        "create_time": info.create_time.isoformat() if info is not None and info.create_time is not None else None,
        "original_minds_version": backend_resolver.get_agent_label(agent_id, "original_minds_version"),
        "color": backend_resolver.get_workspace_color(agent_id),
    }


@require_minds_api_key
def _handle_list_workspaces() -> Response:
    """List all workspaces, including destroyed-but-still-backed-up ones."""
    backend_resolver = get_state().backend_resolver
    workspaces = [_serialize_workspace(agent_id) for agent_id in backend_resolver.list_known_workspace_ids()]
    return _json_response({"workspaces": workspaces})


@require_minds_api_key
def _handle_get_workspace(agent_id: str) -> Response:
    """Return the detail summary for one workspace."""
    parsed_id = AgentId(agent_id)
    backend_resolver = get_state().backend_resolver
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)
    return _json_response(_serialize_workspace(parsed_id))


@require_minds_api_key
def _handle_workspace_version(agent_id: str) -> Response:
    """Return version info: the immutable created-at version plus, when online, git-derived current + history.

    ``original_minds_version`` (the create-time label) is always returned.
    ``current_minds_version`` and ``upgrade_merges`` are read from the
    workspace's own git via ``mngr exec`` and are best-effort: an offline
    workspace (or one whose git lacks ``minds-v*`` tags) reports ``null`` /
    ``[]`` for them.
    """
    parsed_id = AgentId(agent_id)
    backend_resolver = get_state().backend_resolver
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)

    original = backend_resolver.get_agent_label(parsed_id, "original_minds_version")
    parent_cg = get_state().root_concurrency_group
    git_version = workspace_version.WorkspaceGitVersion()
    if parent_cg is not None:
        git_version = workspace_version.read_workspace_git_version(
            mngr_binary=get_state().mngr_binary,
            agent_id=parsed_id,
            parent_cg=parent_cg,
        )
    return _json_response(
        {
            "agent_id": str(parsed_id),
            "original_minds_version": original,
            "current_minds_version": git_version.current_minds_version,
            "upgrade_merges": [
                {
                    "commit_sha": merge.commit_sha,
                    "committed_at": merge.committed_at.isoformat() if merge.committed_at is not None else None,
                    "summary": merge.summary,
                }
                for merge in git_version.upgrade_merges
            ],
        }
    )


@require_minds_api_key
def _handle_workspace_backups(agent_id: str) -> Response:
    """List a workspace's restic backup snapshots (works even when it is offline/destroyed)."""
    parsed_id = AgentId(agent_id)
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return _json_error("Backups are not configured", 501)
    try:
        snapshots = backup_status.list_workspace_snapshots(
            paths, parsed_id, parent_cg=get_state().root_concurrency_group
        )
    except BackupProvisioningError as e:
        return _json_error(str(e), 404)
    return _json_response(
        {
            "agent_id": str(parsed_id),
            "snapshots": [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "short_id": snapshot.short_id,
                    "time": snapshot.time.isoformat(),
                    "paths": list(snapshot.paths),
                    "hostname": snapshot.hostname,
                    "tags": list(snapshot.tags),
                    "total_size_bytes": snapshot.total_size_bytes,
                }
                for snapshot in snapshots
            ],
        }
    )


@require_minds_api_key
def _handle_workspace_backup_export(agent_id: str, snapshot_id: str) -> Response:
    """Restore the named snapshot and stream it back as a zip."""
    parsed_id = AgentId(agent_id)
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return _json_error("Backups are not configured", 501)
    backend_resolver = get_state().backend_resolver
    info = backend_resolver.get_agent_display_info(parsed_id)
    host_id = info.host_id if info is not None else str(parsed_id)
    download_label = info.agent_name if info is not None else str(parsed_id)
    try:
        zip_path = export_latest_snapshot_zip(
            paths=paths,
            agent_id=parsed_id,
            host_id=host_id,
            snapshot=snapshot_id,
            parent_cg=get_state().root_concurrency_group,
        )
    except BackupExportError as e:
        return _json_error(str(e), 404)
    except BackupProvisioningError as e:
        logger.warning("Backup export failed for {} snapshot {}: {}", parsed_id, snapshot_id, e)
        return _json_error(str(e), 500)
    return make_file_response(
        path=str(zip_path), media_type="application/zip", filename=f"{download_label}-backup.zip"
    )


# -- Blueprint factory --


def create_api_v1_blueprint() -> Blueprint:
    """Create the /api/v1/ blueprint with all REST API endpoints."""
    blueprint = Blueprint("api_v1", __name__, url_prefix="/api/v1")

    # Notifications (per-agent so the gateway's per-host permission file
    # can restrict each caller to its own agent ids).
    blueprint.add_url_rule("/agents/<agent_id>/notifications", view_func=_handle_notification, methods=["POST"])

    # Cross-workspace management (read surface). Gated by the
    # ``minds-workspaces`` detent scope at the gateway.
    blueprint.add_url_rule("/workspaces", view_func=_handle_list_workspaces, methods=["GET"])
    blueprint.add_url_rule("/workspaces/<agent_id>", view_func=_handle_get_workspace, methods=["GET"])
    blueprint.add_url_rule("/workspaces/<agent_id>/version", view_func=_handle_workspace_version, methods=["GET"])
    blueprint.add_url_rule("/workspaces/<agent_id>/backups", view_func=_handle_workspace_backups, methods=["GET"])
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/backups/<snapshot_id>/export",
        view_func=_handle_workspace_backup_export,
        methods=["POST"],
    )

    return blueprint
