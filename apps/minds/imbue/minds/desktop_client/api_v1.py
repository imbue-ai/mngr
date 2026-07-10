"""REST API v1 blueprint for the minds desktop client.

The central minds API key is the ``Authorization: Bearer <key>`` credential
where ``<key>`` is from :mod:`api_key_store`. The latchkey gateway's bundled
``minds-api-proxy`` extension injects that header on every forwarded request,
so an agent in a workspace reaches us by hitting
``$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/...``.

*Every* ``/api/v1`` route uses one auth implementation
(:func:`require_api_or_cookie_auth`): it accepts *either* that bearer (agents,
via the gateway) *or* the desktop client's signed session cookie, so the browser
UI and in-workspace agents call the same versioned API over one HTTP surface.
Agent reachability of any given route is decided separately, by whether a
``minds-workspaces-<verb>`` schema matches its path at the gateway; routes with
no matching verb (e.g. the ``/desktop`` namespace) are simply unreachable by
agents (deny-all baseline) while still cookie-reachable by the UI.

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
import queue
import shlex
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from typing import Final

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import backup_status
from imbue.minds.desktop_client import backup_update as backup_update_module
from imbue.minds.desktop_client import backup_verification
from imbue.minds.desktop_client import desktop_control
from imbue.minds.desktop_client import destroying
from imbue.minds.desktop_client import workspace_settings
from imbue.minds.desktop_client import workspace_ssh
from imbue.minds.desktop_client import workspace_ssh_tunnel
from imbue.minds.desktop_client import workspace_version
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import LOG_SENTINEL
from imbue.minds.desktop_client.agent_creator import provider_instance_name_for_launch
from imbue.minds.desktop_client.agent_creator import resolve_template_version
from imbue.minds.desktop_client.api_auth import handle_invalid_random_id as _handle_invalid_random_id
from imbue.minds.desktop_client.api_auth import json_error as _json_error
from imbue.minds.desktop_client.api_auth import json_field_error as _json_field_error
from imbue.minds.desktop_client.api_auth import json_response as _json_response
from imbue.minds.desktop_client.api_auth import require_api_or_cookie_auth
from imbue.minds.desktop_client.api_models import AccountSummary
from imbue.minds.desktop_client.api_models import AccountsResponse
from imbue.minds.desktop_client.api_models import AgentNotificationRequest
from imbue.minds.desktop_client.api_models import BackupOperationStatusResponse
from imbue.minds.desktop_client.api_models import BackupServiceConfigureRequest
from imbue.minds.desktop_client.api_models import BackupServiceUpdateRequest
from imbue.minds.desktop_client.api_models import BackupSnapshotSummary
from imbue.minds.desktop_client.api_models import BackupVerificationToggleRequest
from imbue.minds.desktop_client.api_models import BugReportRequest
from imbue.minds.desktop_client.api_models import CreateOperationStatusResponse
from imbue.minds.desktop_client.api_models import CreateWorkspaceRequest
from imbue.minds.desktop_client.api_models import DestroyOperationStatusResponse
from imbue.minds.desktop_client.api_models import EmptyResponse
from imbue.minds.desktop_client.api_models import EnableSharingRequest
from imbue.minds.desktop_client.api_models import EstablishSshRequest
from imbue.minds.desktop_client.api_models import OkResponse
from imbue.minds.desktop_client.api_models import OperationHandleResponse
from imbue.minds.desktop_client.api_models import PatchWorkspaceRequest
from imbue.minds.desktop_client.api_models import ProviderToggleResponse
from imbue.minds.desktop_client.api_models import RestartOperationStatusResponse
from imbue.minds.desktop_client.api_models import RestartWorkspaceRequest
from imbue.minds.desktop_client.api_models import SetProviderEnabledRequest
from imbue.minds.desktop_client.api_models import SharingReadinessResponse
from imbue.minds.desktop_client.api_models import SharingToggleResponse
from imbue.minds.desktop_client.api_models import SshConnectionResponse
from imbue.minds.desktop_client.api_models import StopStateContainerResponse
from imbue.minds.desktop_client.api_models import UpgradeMergeSummary
from imbue.minds.desktop_client.api_models import WorkspaceBackupsResponse
from imbue.minds.desktop_client.api_models import WorkspaceLifecycleResponse
from imbue.minds.desktop_client.api_models import WorkspaceListResponse
from imbue.minds.desktop_client.api_models import WorkspaceSummary
from imbue.minds.desktop_client.api_models import WorkspaceVersionResponse
from imbue.minds.desktop_client.api_spec import API_SPEC
from imbue.minds.desktop_client.api_spec import json_response_model
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import WORKSPACE_DISPLAY_NAME_LABEL
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.backup_export import BackupExportError
from imbue.minds.desktop_client.backup_export import export_snapshot_zip
from imbue.minds.desktop_client.backup_verification_store import is_backup_verification_enabled
from imbue.minds.desktop_client.backup_verification_store import set_backup_verification_enabled
from imbue.minds.desktop_client.create_helpers import REMOTE_SIGNIN_REDIRECT_URL
from imbue.minds.desktop_client.create_helpers import color_for_new_workspace
from imbue.minds.desktop_client.create_helpers import existing_workspace_host_names
from imbue.minds.desktop_client.create_helpers import taken_host_names_on_provider
from imbue.minds.desktop_client.help_modal_requests import OpenHelpRequest
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.responses import make_file_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.responses import make_streaming_response
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import disable_sharing
from imbue.minds.desktop_client.sharing_handler import enable_sharing_via_cloudflare
from imbue.minds.desktop_client.sharing_handler import get_sharing_status
from imbue.minds.desktop_client.sharing_handler import is_probeable_share_url
from imbue.minds.desktop_client.sharing_handler import probe_share_url_readiness
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.templates import FALLBACK_BRANCH
from imbue.minds.desktop_client.templates import normalize_host_name_slug
from imbue.minds.desktop_client.templates import resolve_create_host_name
from imbue.minds.desktop_client.templates import status_text_for
from imbue.minds.desktop_client.workspace_create import build_backup_request_or_error
from imbue.minds.desktop_client.workspace_create import build_create_on_created_callback
from imbue.minds.desktop_client.workspace_create import resolve_effective_region
from imbue.minds.desktop_client.workspace_lifecycle import MindHostAction
from imbue.minds.desktop_client.workspace_lifecycle import perform_mind_host_action
from imbue.minds.desktop_client.workspace_operations import OPERATION_LOG_SENTINEL
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.desktop_client.workspace_recovery import RestartWorkerFailureHandler
from imbue.minds.desktop_client.workspace_recovery import probe_workspace_health
from imbue.minds.desktop_client.workspace_recovery import run_restart_sequence
from imbue.minds.envs.docker_cleanup import DockerCleanupError
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.errors import MngrCommandError
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import InvalidName

# A blocking lifecycle (start/stop) call shells out to ``mngr`` and waits for
# the host transition to resolve before returning the final state.
_LIFECYCLE_TIMEOUT_SECONDS: float = 300.0

# SSE event-stream headers (disable proxy/browser buffering so events flush live).
_SSE_HEADERS: dict[str, str] = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
# Poll cadence for tailing a destroy operation's on-disk log.
_DESTROY_LOG_POLL_SECONDS: float = 1.0


# -- Notification route --


@require_api_or_cookie_auth
@API_SPEC.validate(json=AgentNotificationRequest, resp=json_response_model(OkResponse))
def _handle_notification(agent_id: str) -> OkResponse | Response:
    """Send a notification on behalf of the named agent."""
    dispatcher: NotificationDispatcher | None = get_state().notification_dispatcher
    if dispatcher is None:
        return _json_error("Notification dispatch not configured", 501)

    # Structure (object shape + ``message`` present and a string) is enforced by
    # the spectree model; the remaining checks here are value-semantic.
    body = request.get_json(silent=True, force=True) or {}
    message = body.get("message")
    if not message:
        return _json_error("'message' field is required and must be a string", 400)

    title = body.get("title")
    urgency_str = body.get("urgency") or "NORMAL"
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
    return OkResponse(ok=True)


# -- Cross-workspace management routes --
#
# These let an agent in one workspace act on *other* workspaces (and their
# backups) through the hub. Every route is gated at the gateway by the
# ``minds-workspaces`` detent scope (see ``mngr_latchkey.agent_setup``); the
# scope's per-verb permissions decide which of these a given caller may reach.
# A workspace is addressed by its primary (``is_primary``+``workspace``) agent
# id, matching minds discovery.


def _serialize_workspace(agent_id: AgentId) -> WorkspaceSummary:
    """Build the summary for one workspace from discovery + its labels."""
    state = get_state()
    backend_resolver = state.backend_resolver
    # The owning signed-in account (None when private or no session store), so the
    # detail readout can confirm an association rather than leaving it invisible.
    account = state.session_store.get_account_for_workspace(str(agent_id)) if state.session_store is not None else None
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
    return WorkspaceSummary(
        agent_id=str(agent_id),
        # The human-readable display name (``workspace_display_name`` label,
        # falling back to the host name for legacy workspaces). Never the agent
        # name, which is the constant ``system-services``.
        name=backend_resolver.get_workspace_name(agent_id),
        host_id=host_id,
        host_state=str(host_state) if host_state is not None else None,
        git_url=backend_resolver.get_agent_label(agent_id, "remote"),
        branch=backend_resolver.get_agent_label(agent_id, "original_branch"),
        account_id=account.user_id if account is not None else None,
        account_email=account.email if account is not None else None,
        provider_name=info.provider_name if info is not None else None,
        create_time=info.create_time.isoformat() if info is not None and info.create_time is not None else None,
        original_minds_version=backend_resolver.get_agent_label(agent_id, "original_minds_version"),
        color=backend_resolver.get_workspace_color(agent_id),
    )


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(WorkspaceListResponse))
def _handle_list_workspaces() -> WorkspaceListResponse:
    """List all workspaces, including destroyed-but-still-backed-up ones."""
    backend_resolver = get_state().backend_resolver
    workspaces = tuple(_serialize_workspace(agent_id) for agent_id in backend_resolver.list_known_workspace_ids())
    return WorkspaceListResponse(workspaces=workspaces)


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(AccountsResponse))
def _handle_list_accounts() -> AccountsResponse:
    """List the accounts signed in on this device (id + email + display name).

    Lets a caller turn a known email into the account id the
    workspace-association API (``PATCH /api/v1/workspaces/<id>``) accepts. Empty
    when no session store is configured. This route is gated by the
    ``minds-accounts-read`` permission, which is NOT in the agent baseline -- an
    agent must be granted it explicitly before it can enumerate accounts.
    """
    session_store = get_state().session_store
    accounts = (
        tuple(
            AccountSummary(account_id=account.user_id, email=account.email, display_name=account.display_name)
            for account in session_store.list_accounts()
        )
        if session_store is not None
        else ()
    )
    return AccountsResponse(accounts=accounts)


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(WorkspaceSummary))
def _handle_get_workspace(agent_id: str) -> WorkspaceSummary | Response:
    """Return the detail summary for one workspace."""
    parsed_id = AgentId(agent_id)
    backend_resolver = get_state().backend_resolver
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)
    return _serialize_workspace(parsed_id)


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(WorkspaceVersionResponse))
def _handle_workspace_version(agent_id: str) -> WorkspaceVersionResponse | Response:
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
    return WorkspaceVersionResponse(
        agent_id=str(parsed_id),
        original_minds_version=original,
        current_minds_version=git_version.current_minds_version,
        upgrade_merges=tuple(
            UpgradeMergeSummary(
                commit_sha=merge.commit_sha,
                committed_at=merge.committed_at.isoformat() if merge.committed_at is not None else None,
                summary=merge.summary,
            )
            for merge in git_version.upgrade_merges
        ),
    )


# Exit budget for the per-request concurrency group in the backups route. The
# check thread is bounded by the check's own exec timeout, so this margin
# guarantees the group exit waits the check out (a slow check delays the
# response) instead of timing out the strand and turning the route into a 500.
_BACKUP_DETAIL_EXIT_TIMEOUT_SECONDS: Final[float] = backup_verification.CHECK_EXEC_TIMEOUT_SECONDS + 30.0


class _WorkspaceSnapshotListing(FrozenModel):
    """The snapshot half of the per-workspace backups response."""

    snapshots: tuple[BackupSnapshotSummary, ...] = Field(description="All snapshots, newest-first")
    is_backing_up: bool = Field(description="Whether a (non-stale) restic backup is currently running")
    error: str | None = Field(default=None, description="Why the listing failed, when it did")


def _list_workspace_snapshots_safely(paths: WorkspacePaths, parsed_id: AgentId) -> _WorkspaceSnapshotListing:
    """List a workspace's snapshots + in-progress flag, degrading errors into the payload.

    An unconfigured workspace (no canonical env) is an ordinary empty listing,
    not an error -- NOT_CONFIGURED surfaces through the verification half.
    """
    if not has_canonical_env(paths, parsed_id):
        return _WorkspaceSnapshotListing(snapshots=(), is_backing_up=False)
    try:
        snapshots = backup_status.list_workspace_snapshots(
            paths, parsed_id, parent_cg=get_state().root_concurrency_group
        )
    except BackupProvisioningError as e:
        logger.warning("Backup snapshot listing failed for {}: {}", parsed_id, e)
        return _WorkspaceSnapshotListing(snapshots=(), is_backing_up=False, error=str(e))
    # Whether a backup is running *right now* (non-stale restic lock). The
    # snapshot list alone can't express this, so the landing page reads this
    # flag to show the live "Backing up..." badge.
    is_backing_up = backup_status.is_workspace_backing_up(
        paths, parsed_id, now=datetime.now(timezone.utc), parent_cg=get_state().root_concurrency_group
    )
    return _WorkspaceSnapshotListing(
        snapshots=tuple(
            BackupSnapshotSummary(
                snapshot_id=snapshot.snapshot_id,
                short_id=snapshot.short_id,
                time=snapshot.time.isoformat(),
                paths=tuple(snapshot.paths),
                hostname=snapshot.hostname,
                tags=tuple(snapshot.tags),
                total_size_bytes=snapshot.total_size_bytes,
            )
            for snapshot in snapshots
        ),
        is_backing_up=is_backing_up,
    )


def _check_backup_service_safely(
    paths: WorkspacePaths,
    parsed_id: AgentId,
    # Resolved on the request thread and passed explicitly: this runs on a
    # concurrency-group thread, where the Flask app-context state proxy
    # (get_state) is unavailable.
    resolver: BackendResolverInterface,
    parent_cg: ConcurrencyGroup | None,
) -> "backup_verification.BackupServiceCheck":
    """Run the backup-service check, degrading a crash to UNKNOWN (no badge)."""
    try:
        return backup_verification.check_backup_service_for_workspace(
            paths, parsed_id, resolver=resolver, parent_cg=parent_cg
        )
    except BackupProvisioningError as e:
        # A real error (e.g. the adoption write to the canonical env store
        # failed); the response still degrades to UNKNOWN rather than failing.
        logger.warning("Backup service check for {} failed: {}", parsed_id, e)
        return backup_verification.BackupServiceCheck(state=backup_verification.BackupServiceCheckState.UNKNOWN)


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(WorkspaceBackupsResponse))
def _handle_workspace_backups(agent_id: str) -> WorkspaceBackupsResponse | Response:
    """One workspace's full backup picture: snapshots + service verification.

    The restic snapshot listing (run from this machine; works even when the
    workspace is offline or destroyed) and the backup-service check (an exec
    into the workspace) run concurrently -- the check on a concurrency-group
    thread, the listing on the request thread. Cross-workspace parallelism is
    the frontend's job: it fans out one request per workspace.
    """
    parsed_id = AgentId(agent_id)
    state = get_state()
    paths: WorkspacePaths | None = state.api_v1_paths
    if paths is None:
        return _json_error("Backups are not configured", 501)

    check_results: list[backup_verification.BackupServiceCheck] = []
    resolver = state.backend_resolver
    parent_cg = state.root_concurrency_group

    def _run_check_into_results() -> None:
        check_results.append(_check_backup_service_safely(paths, parsed_id, resolver, parent_cg))

    cg_name = f"backup-detail-{parsed_id}"
    cg = (
        parent_cg.make_concurrency_group(name=cg_name, exit_timeout_seconds=_BACKUP_DETAIL_EXIT_TIMEOUT_SECONDS)
        if parent_cg is not None
        else ConcurrencyGroup(name=cg_name, exit_timeout_seconds=_BACKUP_DETAIL_EXIT_TIMEOUT_SECONDS)
    )
    with cg:
        cg.start_new_thread(target=_run_check_into_results, name=f"backup-check-{parsed_id}")
        listing = _list_workspace_snapshots_safely(paths, parsed_id)
    check = (
        check_results[0]
        if check_results
        else backup_verification.BackupServiceCheck(state=backup_verification.BackupServiceCheckState.UNKNOWN)
    )

    return WorkspaceBackupsResponse(
        agent_id=str(parsed_id),
        is_configured=has_canonical_env(paths, parsed_id),
        is_backing_up=listing.is_backing_up,
        snapshots=listing.snapshots,
        snapshots_error=listing.error,
        check_state=check.state.value,
        problems=tuple(problem.value for problem in check.problems),
        installed_version=check.installed_version,
        minimum_version=check.minimum_version,
        update_target_version=backup_verification.update_target_backup_tag(),
        check_detail=check.detail,
        is_verification_enabled=is_backup_verification_enabled(paths, parsed_id),
    )


@require_api_or_cookie_auth
def _handle_workspace_backup_export(agent_id: str, snapshot_id: str) -> Response:
    """Restore the named snapshot (or ``latest``) and stream it back as a zip.

    ``snapshot_id`` is passed to restic verbatim, so restic's own snapshot
    addressing applies -- in particular ``latest`` exports the newest snapshot
    without the caller having to list them first.
    """
    parsed_id = AgentId(agent_id)
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return _json_error("Backups are not configured", 501)
    backend_resolver = get_state().backend_resolver
    info = backend_resolver.get_agent_display_info(parsed_id)
    host_id = info.host_id if info is not None else str(parsed_id)
    download_label = info.agent_name if info is not None else str(parsed_id)
    try:
        zip_path = export_snapshot_zip(
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


# -- Cross-workspace mutation routes (create / destroy / lifecycle) --


@require_api_or_cookie_auth
@API_SPEC.validate(json=CreateWorkspaceRequest, resp=json_response_model(OperationHandleResponse, status_code=202))
def _handle_create_workspace() -> tuple[OperationHandleResponse, int] | Response:
    """Create a new peer workspace; return an operation handle to poll.

    Accepts a JSON body with ``git_url`` (required) and optional ``host_name``,
    ``branch``, ``color``, ``launch_mode`` (default ``DOCKER``), ``ai_provider``
    (default ``SUBSCRIPTION``), ``account_id`` (selects the imbue_cloud account
    for compute/AI -- required when ``launch_mode`` or ``ai_provider`` is
    ``IMBUE_CLOUD``), ``anthropic_api_key`` (required when ``ai_provider`` is
    ``API_KEY``), and ``region``. Returns ``202`` with an ``operation_id`` the
    caller polls at ``/api/v1/workspaces/operations/create/<operation_id>``; the
    canonical workspace id appears there once ``mngr create`` returns.

    This is the single create front door for both agents and the browser. To let
    the create page render validation errors inline, a ``400`` carries a
    structured body: ``{"error", "field"}`` names the offending field where
    applicable (agents ignore ``field``), and the no-account imbue_cloud backstop
    returns ``{"error", "redirect_url"}`` pointing at the sign-up flow. An empty
    ``host_name`` is auto-resolved to the next free ``workspace-N`` (the form no
    longer asks for a name).

    Backup provisioning and Cloudflare tunnel injection match the desktop UI's
    create flow: the optional ``backup_*`` fields (``backup_provider``,
    ``backup_master_password``, ``backup_save_password``,
    ``backup_api_key_env``) build the same restic
    setup request, and -- when an ``account_id`` is given -- the same
    post-creation callback associates the peer with the account and injects a
    Cloudflare tunnel token. Both reuse the shared helpers in
    ``workspace_create`` so the two front doors stay in lockstep.
    """
    agent_creator: AgentCreator | None = get_state().agent_creator
    if agent_creator is None:
        return _json_error("Agent creation not configured", 501)

    # Object shape + ``git_url`` presence/type are enforced by the spectree model;
    # the value-semantic checks below (empty-after-strip, provider rules) stay here.
    body = request.get_json(silent=True, force=True) or {}
    git_url = str(body.get("git_url", "")).strip()
    if not git_url:
        return _json_field_error("Repository URL is required.", "git_url")
    host_name = str(body.get("host_name", "")).strip()
    branch = str(body.get("branch", "")).strip()
    color = color_for_new_workspace(body.get("color"))
    try:
        launch_mode = LaunchMode(str(body.get("launch_mode", LaunchMode.DOCKER.value)))
    except ValueError:
        return _json_error(f"Invalid launch_mode: {body.get('launch_mode')!r}", 400)
    try:
        ai_provider = AIProvider(str(body.get("ai_provider", AIProvider.SUBSCRIPTION.value)))
    except ValueError:
        return _json_error(f"Invalid ai_provider: {body.get('ai_provider')!r}", 400)
    try:
        backup_provider = BackupProvider(str(body.get("backup_provider", BackupProvider.CONFIGURE_LATER.value)))
    except ValueError:
        return _json_error(f"Invalid backup_provider: {body.get('backup_provider')!r}", 400)
    # Wrapped in SecretStr immediately so the plaintext never rides a local
    # that could end up in a log or an error message.
    backup_master_password = SecretStr(str(body.get("backup_master_password") or ""))
    is_save_backup_password = bool(body.get("backup_save_password", False))
    backup_api_key_env = str(body.get("backup_api_key_env", ""))
    account_id = str(body.get("account_id", "")).strip()
    anthropic_api_key = str(body.get("anthropic_api_key", "")).strip()
    submitted_region = str(body.get("region", "")).strip()

    # The workspace name is chosen automatically unless one was submitted (the
    # advanced view's optional "Name" field): a submitted value, else the next
    # free ``workspace-N`` name (computed from the host names already in use across
    # every provider). Resolve it eagerly so an invalid name surfaces as a 400
    # here rather than as a deferred FAILED status on the creating page.
    backend_resolver = get_state().backend_resolver
    try:
        resolved_host_name = resolve_create_host_name(host_name, existing_workspace_host_names(backend_resolver))
    except InvalidName as exc:
        return _json_field_error(str(exc), "host_name")

    # Mirror the UI's create-form validation so misconfiguration fails fast here
    # rather than deep in the background creation thread.
    session_store: MultiAccountSessionStore | None = get_state().session_store
    is_imbue_cloud = launch_mode is LaunchMode.IMBUE_CLOUD or ai_provider is AIProvider.IMBUE_CLOUD
    if is_imbue_cloud and not account_id:
        # The remote (Imbue Cloud) presets require an account. With no account at
        # all the compute path is unusable, so carry the sign-up redirect target
        # back to the create page (its no-JS backstop is the same destination).
        # When accounts exist but none is selected, ask the user to pick one.
        has_any_account = bool(session_store.list_accounts()) if session_store is not None else False
        if not has_any_account:
            return _json_response(
                {
                    "error": "imbue_cloud requires an account. Sign in to continue.",
                    "redirect_url": REMOTE_SIGNIN_REDIRECT_URL,
                },
                status_code=400,
            )
        return _json_field_error(
            "imbue_cloud requires an account. Select an account or pick a different "
            "option for both the compute and AI providers.",
            "account_id",
        )
    if ai_provider is AIProvider.API_KEY and not anthropic_api_key:
        return _json_field_error(
            "An Anthropic API key is required when AI provider is set to api_key.", "anthropic_api_key"
        )

    # Resolve the imbue_cloud account email (the session store maps account_id
    # -> email) so the background creation can mint a LiteLLM key / lease a pool
    # host against the right account.
    account_email = ""
    if account_id and session_store is not None:
        account_email = session_store.get_account_email(account_id) or ""

    # Build the same restic setup request the create form builds (validates the
    # master password against the stored hash; optionally saves the plaintext
    # convenience copy). Fail fast on a bad config.
    backup_request, backup_error = build_backup_request_or_error(
        backup_provider=backup_provider,
        typed_master_password=backup_master_password,
        is_save_password=is_save_backup_password,
        api_key_env=backup_api_key_env,
        account_email=account_email,
        paths=agent_creator.paths,
    )
    if backup_error is not None:
        return _json_field_error(backup_error, "backup_master_password")

    # For imbue_cloud compute the lease needs the resolved template version
    # (the latest semver tag when no branch was given), matching the form path.
    branch_or_tag = branch
    if launch_mode is LaunchMode.IMBUE_CLOUD and not branch_or_tag:
        branch_or_tag = resolve_template_version(git_url, branch, parent_cg=agent_creator.root_concurrency_group)

    # Resolve the effective region (honoring a valid submitted value, else the
    # provider default) and, on a successful create, build the post-creation
    # callback that injects the Cloudflare tunnel token + associates the account
    # and persists the chosen region -- exactly as the create form does.
    minds_config = get_state().minds_config
    region = resolve_effective_region(launch_mode, submitted_region, minds_config, get_state().geo_location_cache)
    on_created = build_create_on_created_callback(account_id, minds_config, launch_mode, region)

    creation_id = agent_creator.start_creation(
        git_url,
        host_name=resolved_host_name,
        # The raw, arbitrary name the user typed becomes the display name; the
        # resolved slug above is the host name. When blank, start_creation falls
        # the display name back to the slug.
        display_name=host_name,
        branch=branch,
        launch_mode=launch_mode,
        ai_provider=ai_provider,
        account_email=account_email,
        branch_or_tag=branch_or_tag,
        region=region,
        anthropic_api_key=anthropic_api_key,
        on_created=on_created,
        backup_request=backup_request,
        color=color,
        original_minds_version=(branch_or_tag or branch or FALLBACK_BRANCH),
    )
    return OperationHandleResponse(operation_id=str(creation_id), kind="create"), 202


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(OperationHandleResponse, status_code=202))
def _handle_destroy_workspace(agent_id: str) -> tuple[OperationHandleResponse, int] | Response:
    """Destroy a workspace's host; return an operation handle to poll.

    The workspace's backups and ``restic.env`` are retained, so its backups
    stay listable/exportable after destruction.
    """
    parsed_id = AgentId(agent_id)
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return _json_error("Workspace management not configured", 501)
    backend_resolver = get_state().backend_resolver
    info = backend_resolver.get_agent_display_info(parsed_id)
    if info is None:
        return _json_error(f"Unknown workspace {agent_id}", 404)
    try:
        host_id = HostId(info.host_id)
    except ValueError:
        return _json_error(f"Cannot resolve a host to destroy for {agent_id}", 409)

    destroying.start_destroy(parsed_id, paths, host_id, mngr_binary=get_state().mngr_binary)
    return OperationHandleResponse(operation_id=str(parsed_id), kind="destroy"), 202


def _run_mngr_blocking(argv: list[str], parent_cg: ConcurrencyGroup) -> tuple[int, str, str]:
    """Run an ``mngr`` command to completion; return ``(returncode, stdout, stderr)``."""
    cg = parent_cg.make_concurrency_group(name="workspace-lifecycle")
    with cg:
        finished = cg.run_process_to_completion(argv, timeout=_LIFECYCLE_TIMEOUT_SECONDS, is_checked_after=False)
    returncode = finished.returncode if finished.returncode is not None else 1
    return returncode, finished.stdout, finished.stderr


def _perform_workspace_lifecycle(agent_id: str, action: str) -> WorkspaceLifecycleResponse | Response:
    """Shared start/stop implementation; the two routes are thin named wrappers (so each is documented)."""
    parsed_id = AgentId(agent_id)
    parent_cg = get_state().root_concurrency_group
    if parent_cg is None:
        return _json_error("Workspace lifecycle not configured", 501)
    backend_resolver = get_state().backend_resolver
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)

    # Shared with the browser landing controls: resolves the workspace to its
    # system-services agent, runs mngr stop --stop-host / start, and sets the
    # optimistic host-state override on success.
    host_action = MindHostAction.START if action == "start" else MindHostAction.STOP
    succeeded = perform_mind_host_action(
        parsed_id,
        host_action,
        backend_resolver,
        get_state().mngr_binary,
        get_state().mngr_host_dir,
        parent_cg,
    )
    if not succeeded:
        return _json_error(f"Could not {action} the workspace host", 502)

    info = backend_resolver.get_agent_display_info(parsed_id)
    host_state = None
    if info is not None:
        try:
            host_state = backend_resolver.get_host_state(HostId(info.host_id))
        except ValueError:
            host_state = None
    return WorkspaceLifecycleResponse(
        agent_id=str(parsed_id),
        action=action,
        host_state=str(host_state) if host_state is not None else None,
    )


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(WorkspaceLifecycleResponse))
def _handle_workspace_start(agent_id: str) -> WorkspaceLifecycleResponse | Response:
    """Start a workspace's host, blocking until the transition resolves."""
    return _perform_workspace_lifecycle(agent_id, "start")


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(WorkspaceLifecycleResponse))
def _handle_workspace_stop(agent_id: str) -> WorkspaceLifecycleResponse | Response:
    """Stop a workspace's host, blocking until the transition resolves."""
    return _perform_workspace_lifecycle(agent_id, "stop")


def _apply_workspace_display_label(
    agent_id: AgentId, display_name: str, host_name_slug: str | None, parent_cg: ConcurrencyGroup
) -> Response:
    """Write the workspace's human-readable display label, returning the API response.

    ``host_name_slug`` is the workspace's new normalized host name when the rename
    also renamed the host (a slug change), or None for a display-only rename.
    """
    returncode, _stdout, stderr = _run_mngr_blocking(
        [get_state().mngr_binary, "label", str(agent_id), "--label", f"{WORKSPACE_DISPLAY_NAME_LABEL}={display_name}"],
        parent_cg,
    )
    if returncode != 0:
        return _json_error(f"Failed to update workspace name: {stderr.strip()[:200]}", 502)
    # Optimistically reflect the just-persisted name in the discovery-fed resolver
    # cache so an immediate settings reload renders the new name instead of the stale
    # one; discovery re-reads the label on its next snapshot and reconciles (or
    # expires) the override.
    get_state().backend_resolver.set_workspace_name_override(agent_id, display_name, host_name_slug)
    return _json_response({"agent_id": str(agent_id), "name": display_name})


@require_api_or_cookie_auth
def _handle_workspace_rename(agent_id: str) -> Response:
    """Rename a workspace (``POST .../workspaces/<agent_id>/rename``).

    Updates the workspace's normalized host name (the slug) and its
    human-readable display label together so the two never drift. When the new
    name normalizes to the same slug as the current host name, only the display
    label is rewritten -- no host rename, so it works on every provider and
    while offline. ``agent_id`` is the workspace's ``system-services`` agent id.
    """
    parsed_id = AgentId(agent_id)
    state = get_state()
    backend_resolver = state.backend_resolver
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)
    parent_cg = state.root_concurrency_group
    if parent_cg is None:
        return _json_error("Workspace rename is unavailable in this configuration", 503)

    raw_name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    if not raw_name:
        return _json_field_error("A workspace name is required.", "name")
    try:
        new_slug = normalize_host_name_slug(raw_name)
    except InvalidName as exc:
        return _json_field_error(str(exc), "name")

    current_host_name = backend_resolver.get_host_name(parsed_id)

    # Display-only rename: the slug is unchanged, so just rewrite the label
    # (no host rename needed -- works on every provider, online or offline).
    if current_host_name is not None and new_slug.casefold() == current_host_name.casefold():
        return _apply_workspace_display_label(parsed_id, raw_name, None, parent_cg)

    # Reject a slug that collides with another active workspace on the same provider.
    info = backend_resolver.get_agent_display_info(parsed_id)
    if info is not None and info.provider_name is not None:
        taken = taken_host_names_on_provider(backend_resolver, info.provider_name)
        if current_host_name is not None:
            taken.discard(current_host_name.casefold())
        if new_slug.casefold() in taken:
            return _json_error(f"A workspace named '{new_slug}' already exists.", 409)

    # Rename the host first, then update the display label. The operation is
    # idempotently re-runnable: re-running completes an interrupted rename.
    returncode, _stdout, stderr = _run_mngr_blocking(
        [state.mngr_binary, "rename", "--host", str(parsed_id), str(new_slug)], parent_cg
    )
    if returncode != 0:
        return _json_error(f"Failed to rename workspace host: {stderr.strip()[:200]}", 502)
    return _apply_workspace_display_label(parsed_id, raw_name, str(new_slug), parent_cg)


# -- Workspace recovery routes (health probe + restart) --


@require_api_or_cookie_auth
def _handle_workspace_health(agent_id: str) -> Response:
    """Return the workspace's host-health diagnostics (probes + dispatch tier).

    Mirrors the old ``/api/agents/<id>/host-health`` route: a flat
    ``HostHealthResponse`` -- a list of named probes plus a derived
    ``dispatch_tier`` -- that the recovery page renders. 404 if the workspace is
    unknown; 503 if no concurrency group is wired to run the in-container probe.
    """
    parsed_id = AgentId(agent_id)
    state = get_state()
    backend_resolver = state.backend_resolver
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)
    parent_cg = state.root_concurrency_group
    if parent_cg is None:
        return _json_error("Workspace health probe is unavailable in this configuration", 503)
    response = probe_workspace_health(
        parsed_id,
        backend_resolver=backend_resolver,
        tracker=state.system_interface_health_tracker,
        mngr_binary=state.mngr_binary,
        mngr_host_dir=state.mngr_host_dir,
        concurrency_group=parent_cg,
        envelope_stream_consumer=state.envelope_stream_consumer,
    )
    logger.info("Workspace health probe for {}: dispatch_tier={}", parsed_id, response.dispatch_tier.value)
    return make_response(content=response.model_dump_json(), media_type="application/json")


@require_api_or_cookie_auth
@API_SPEC.validate(json=RestartWorkspaceRequest, resp=json_response_model(OperationHandleResponse, status_code=202))
def _handle_workspace_restart(agent_id: str) -> tuple[OperationHandleResponse, int] | Response:
    """Dispatch a workspace restart; return an operation handle to poll.

    Body: ``{"scope": "services" | "host", "host_already_stopped"?: bool}``. The
    ``services`` scope restarts the system-services agent in place; ``host``
    bounces the whole host (``host_already_stopped`` is honored only for the host
    scope, letting a known-stopped host skip the redundant stop step). Returns
    ``202`` with ``{operation_id, kind: "restart"}`` (the op id is the workspace
    agent id), followed via ``/api/v1/workspaces/operations/restart/<id>``
    (+``/logs``) exactly like create / destroy. A restart already in flight is
    deduped: the same handle is returned without stacking a second worker. A
    RUNNING operation of another kind (a backup update/configure) is a 409:
    workspace operations are serialized, and a restart must not bounce the
    host under an in-flight backup mutation.
    """
    parsed_id = AgentId(agent_id)
    # The spectree model enforces ``scope`` is a required string; its value (one
    # of services/host) is a value-semantic check kept here.
    body = request.get_json(silent=True, force=True) or {}
    scope = body.get("scope")
    if scope not in ("services", "host"):
        return _json_error("'scope' must be one of: services, host", 400)
    is_host_restart = scope == "host"

    state = get_state()
    backend_resolver = state.backend_resolver
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)
    tracker: SystemInterfaceHealthTracker | None = state.system_interface_health_tracker
    parent_cg = state.root_concurrency_group
    if tracker is None or parent_cg is None:
        return _json_error("Workspace restart is unavailable in this configuration", 503)

    handle = OperationHandleResponse(operation_id=str(parsed_id), kind="restart")
    # An auto-dispatched recovery restart (fired by the recovery page off its tier
    # classification) can race the workspace's own self-recovery: the host-health
    # probe that picks the tier runs a slow in-container exec, and the background
    # probe loop can flip the tracker back to HEALTHY while that exec is still in
    # flight. Restarting a workspace that already recovered is pure harm -- it
    # bounces a healthy backend for nothing -- so skip it and let the recovery
    # page's refresh 302 the user back to the workspace. A manual restart carries
    # no marker and always proceeds; the user explicitly asked.
    if bool(body.get("auto_dispatched", False)) and tracker.get_health(parsed_id) == AgentHealth.HEALTHY:
        logger.info(
            "Skipping auto-dispatched {} restart for {}: workspace already recovered to HEALTHY "
            "before the recovery probe completed",
            scope,
            parsed_id,
        )
        return handle, 202
    # Serialize with the backup operations: ``registry.start`` below replaces
    # the workspace's record, so a RUNNING backup update/configure must be
    # rejected here (its worker's terminal complete/fail would corrupt the
    # restart's record, and restarting would bounce the host under an
    # in-flight backup mutation). The backup dispatch routes reject in the
    # other direction via their atomic ``start_if_idle``.
    registry = state.workspace_operation_registry
    existing_operation = registry.get(parsed_id)
    if (
        existing_operation is not None
        and existing_operation.status == WorkspaceOperationStatus.RUNNING
        and existing_operation.kind != WorkspaceOperationKind.RESTART
    ):
        return _json_error(
            f"Another operation ({existing_operation.kind.value}) is already running for {agent_id}", 409
        )
    # A restart already in flight for this workspace -- don't stack a second
    # worker racing the first's stop/start commands. mark_restarting decides the
    # RESTARTING transition under its own lock and reports whether this caller won
    # it, so this is an atomic check-and-claim against concurrent requests.
    if not tracker.mark_restarting(parsed_id):
        return handle, 202

    registry.start(parsed_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))

    # host_already_stopped lets an auto-dispatched host restart skip the redundant
    # stop step; honored only for host restarts (a manual restart may target a
    # still-running container, which must be stopped first).
    skip_stop = is_host_restart and bool(body.get("host_already_stopped", False))

    # is_checked=False + on_failure: a crash of the one-shot worker transitions
    # the tracker to RESTART_FAILED and the registry to FAILED (so neither the
    # recovery page nor the operation poller hangs). The spawn itself can also
    # raise when the group is shutting down; since we've already claimed
    # RESTARTING, roll both into the failed state and report 503.
    try:
        parent_cg.start_new_thread(
            target=run_restart_sequence,
            kwargs={
                "workspace_agent_id": parsed_id,
                "is_host_restart": is_host_restart,
                "tracker": tracker,
                "backend_resolver": backend_resolver,
                "mngr_binary": state.mngr_binary,
                "mngr_host_dir": state.mngr_host_dir,
                "concurrency_group": parent_cg,
                "mngr_forward_port": state.mngr_forward_port or 0,
                "mngr_forward_preauth_cookie": state.mngr_forward_preauth_cookie,
                "registry": registry,
                "skip_stop": skip_stop,
            },
            name=f"workspace-restart-{parsed_id}",
            daemon=True,
            is_checked=False,
            on_failure=RestartWorkerFailureHandler(tracker=tracker, workspace_agent_id=parsed_id, registry=registry),
        )
    except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
        logger.warning("Failed to spawn restart worker for {}: {}", parsed_id, exc)
        message = f"Could not start the restart worker: {exc}"
        tracker.mark_restart_failed(parsed_id, message)
        registry.fail(parsed_id, message)
        return _json_error(message, 503)
    return handle, 202


# Operation polling is segmented by type -- ``/operations/<type>/<id>`` -- so the
# id no longer has to be disambiguated by prefix, and a destroy and a restart of
# the same workspace (both keyed by the agent id) can't shadow each other. The
# caller always knows the type (the creating / destroying / recovery flows each
# poll their own), so each type has its own handler + precise response model.


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(CreateOperationStatusResponse))
def _handle_create_operation_status(operation_id: str) -> CreateOperationStatusResponse | Response:
    """Report the status of a create operation (the id is a ``creation-...`` id)."""
    agent_creator: AgentCreator | None = get_state().agent_creator
    info = agent_creator.get_creation_info(CreationId(operation_id)) if agent_creator is not None else None
    if info is None:
        return _json_error(f"Unknown operation {operation_id}", 404)
    return CreateOperationStatusResponse(
        operation_id=operation_id,
        kind="create",
        status=str(info.status),
        # Human-readable stage caption for the creating page (e.g. "Cloning
        # repository...", "Failed: ..."), mode-aware. Restores the live caption
        # the old per-stage SSE status frames carried.
        status_text=status_text_for(str(info.status), error=info.error, launch_mode=info.launch_mode),
        is_done=info.status == AgentCreationStatus.DONE,
        agent_id=str(info.agent_id) if info.agent_id is not None else None,
        # The absolute ``/goto/<agent>/`` URL the creating page navigates to once
        # the workspace is ready. Built by the creator (it knows the ``mngr
        # forward`` port) and populated atomically with DONE, so the page
        # redirects without reconstructing it client-side.
        redirect_url=info.redirect_url,
        error=info.error,
    )


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(DestroyOperationStatusResponse))
def _handle_destroy_operation_status(operation_id: str) -> DestroyOperationStatusResponse | Response:
    """Report the status of a destroy operation (the id is the workspace agent id)."""
    parsed_id = AgentId(operation_id)
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return _json_error(f"Unknown operation {operation_id}", 404)
    backend_resolver = get_state().backend_resolver
    # A destroy is only DONE once the workspace's *host* is gone (not merely the
    # workspace agent): a destroy that tore down only the agent while the host's
    # ``system-services`` kept it alive must read as FAILED, not a false DONE.
    # ``destroying.is_host_still_active`` answers that (active-set membership OR a
    # host not yet in ``DESTROYED``); see :func:`destroying.read_destroying`.
    record = destroying.read_destroying(
        parsed_id, paths, destroying.is_host_still_active(backend_resolver, paths, parsed_id)
    )
    if record is None:
        return _json_error(f"Unknown operation {operation_id}", 404)
    return DestroyOperationStatusResponse(
        operation_id=operation_id,
        kind="destroy",
        status=str(record.status),
        is_done=record.status == destroying.DestroyingStatus.DONE,
        agent_id=operation_id,
    )


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(RestartOperationStatusResponse))
def _handle_restart_operation_status(operation_id: str) -> RestartOperationStatusResponse | Response:
    """Report the status of a restart operation (the id is the workspace agent id)."""
    parsed_id = AgentId(operation_id)
    restart_record = get_state().workspace_operation_registry.get(parsed_id)
    # Operation polling is type-segmented: a backup update/configure record for
    # the same workspace must not read as a restart through this endpoint (the
    # backup status handler filters in the same way for the other direction).
    if restart_record is None or restart_record.kind != WorkspaceOperationKind.RESTART:
        return _json_error(f"Unknown operation {operation_id}", 404)
    return RestartOperationStatusResponse(
        operation_id=operation_id,
        kind="restart",
        status=str(restart_record.status),
        is_done=restart_record.status == WorkspaceOperationStatus.DONE,
        error=restart_record.error,
    )


# -- Backup service verification + management routes --


def _resolve_backup_route_context(agent_id: str) -> "tuple[AgentId, WorkspacePaths, ConcurrencyGroup] | Response":
    """Shared 404/503 gating for the backup-service mutation routes."""
    parsed_id = AgentId(agent_id)
    state = get_state()
    if parsed_id not in state.backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)
    paths = state.api_v1_paths
    parent_cg = state.root_concurrency_group
    if paths is None or parent_cg is None:
        return _json_error("Backup management is unavailable in this configuration", 503)
    return parsed_id, paths, parent_cg


@require_api_or_cookie_auth
@API_SPEC.validate(json=BackupServiceUpdateRequest, resp=json_response_model(OperationHandleResponse, status_code=202))
def _handle_backup_service_update(agent_id: str) -> tuple[OperationHandleResponse, int] | Response:
    """Dispatch the idempotent 'Update backup service' operation; return a handle to poll.

    Body: ``{"stop_chats"?: bool}`` -- the "Stop all chats and retry" flow sets
    it so actively-RUNNING chat agents are stopped before the code update (they
    resume on the user's next message). One tracked operation runs per
    workspace at a time; a second request while one (of any kind) is running is
    rejected rather than stacked.
    """
    context = _resolve_backup_route_context(agent_id)
    if isinstance(context, Response):
        return context
    parsed_id, paths, parent_cg = context
    state = get_state()
    registry = state.workspace_operation_registry

    body = request.get_json(silent=True, force=True) or {}
    is_stop_chats = bool(body.get("stop_chats", False))
    # Atomic check-and-claim (like restart's mark_restarting): two concurrent
    # requests must not both spawn workers mutating the same workspace.
    if not registry.start_if_idle(parsed_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc)):
        existing = registry.get(parsed_id)
        kind_note = f" ({existing.kind.value})" if existing is not None else ""
        return _json_error(f"Another operation{kind_note} is already running for {agent_id}", 409)
    try:
        parent_cg.start_new_thread(
            target=backup_update_module.run_backup_update_sequence,
            kwargs={
                "agent_id": parsed_id,
                "paths": paths,
                "resolver": state.backend_resolver,
                "registry": registry,
                "parent_cg": parent_cg,
                "is_stop_chats": is_stop_chats,
            },
            name=f"backup-update-{parsed_id}",
            daemon=True,
            is_checked=False,
            on_failure=backup_update_module.BackupWorkerFailureHandler(
                workspace_agent_id=parsed_id, registry=registry
            ),
        )
    except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
        logger.warning("Failed to spawn backup update worker for {}: {}", parsed_id, exc)
        message = f"Could not start the backup update worker: {exc}"
        registry.fail(parsed_id, message)
        return _json_error(message, 503)
    return OperationHandleResponse(operation_id=str(parsed_id), kind="backup_update"), 202


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(EmptyResponse))
def _handle_backup_service_update_cancel(agent_id: str) -> EmptyResponse | Response:
    """Cancel a waiting backup update (only effective before it starts mutating)."""
    parsed_id = AgentId(agent_id)
    registry = get_state().workspace_operation_registry
    record = registry.get(parsed_id)
    if record is None or record.kind != WorkspaceOperationKind.BACKUP_UPDATE:
        return _json_error(f"No backup update operation for {agent_id}", 404)
    registry.request_cancel(parsed_id)
    return EmptyResponse()


@require_api_or_cookie_auth
@API_SPEC.validate(
    json=BackupServiceConfigureRequest, resp=json_response_model(OperationHandleResponse, status_code=202)
)
def _handle_backup_service_configure(agent_id: str) -> tuple[OperationHandleResponse, int] | Response:
    """Enable backups on a workspace, or change where its backups go.

    Both are the same idempotent fresh-provisioning path: when a canonical env
    already exists it is archived first (destination change; the old repository
    stays reachable through the archive), then the ordinary provisioning runs
    against the new inputs and injects the rotated env. Env-only -- never
    touches the repo, so no chat gate applies.
    """
    context = _resolve_backup_route_context(agent_id)
    if isinstance(context, Response):
        return context
    parsed_id, paths, parent_cg = context
    state = get_state()
    registry = state.workspace_operation_registry
    # Fast-path rejection before any validation work; the authoritative,
    # race-free claim is the start_if_idle below.
    existing = registry.get(parsed_id)
    if existing is not None and existing.status == WorkspaceOperationStatus.RUNNING:
        return _json_error(f"Another operation ({existing.kind.value}) is already running for {agent_id}", 409)

    body = request.get_json(silent=True, force=True) or {}
    try:
        backup_provider = BackupProvider(str(body.get("backup_provider", "")))
    except ValueError:
        return _json_error("Invalid backup_provider", 400)
    if backup_provider is BackupProvider.CONFIGURE_LATER:
        return _json_error("Pick a real backup provider (imbue_cloud or api_key)", 400)

    display_info = state.backend_resolver.get_agent_display_info(parsed_id)
    if display_info is None:
        return _json_error(f"Workspace {agent_id} has no discovered host", 502)
    account = state.session_store.get_account_for_workspace(str(parsed_id)) if state.session_store else None
    account_email = str(account.email) if account is not None else ""

    backup_request, error_message = build_backup_request_or_error(
        backup_provider=backup_provider,
        typed_master_password=SecretStr(str(body.get("master_password") or "")),
        is_save_password=bool(body.get("save_password", False)),
        api_key_env=str(body.get("api_key_env", "")),
        account_email=account_email,
        paths=paths,
    )
    if backup_request is None or error_message is not None:
        return _json_error(error_message or "Invalid backup configuration", 400)

    is_destination_change = has_canonical_env(paths, parsed_id)
    if not registry.start_if_idle(parsed_id, WorkspaceOperationKind.BACKUP_CONFIGURE, datetime.now(timezone.utc)):
        claimed = registry.get(parsed_id)
        kind_note = f" ({claimed.kind.value})" if claimed is not None else ""
        return _json_error(f"Another operation{kind_note} is already running for {agent_id}", 409)
    registry.append_log(
        parsed_id, "Changing the backup destination..." if is_destination_change else "Enabling backups..."
    )

    try:
        parent_cg.start_new_thread(
            target=backup_update_module.run_backup_configure_sequence,
            kwargs={
                "agent_id": parsed_id,
                "host_id": display_info.host_id,
                "request": backup_request,
                "imbue_cloud_cli": state.imbue_cloud_cli,
                "paths": paths,
                "parent_cg": parent_cg,
                "registry": registry,
                "is_destination_change": is_destination_change,
            },
            name=f"backup-configure-{parsed_id}",
            daemon=True,
            is_checked=False,
            on_failure=backup_update_module.BackupWorkerFailureHandler(
                workspace_agent_id=parsed_id, registry=registry
            ),
        )
    except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
        logger.warning("Failed to spawn backup configure worker for {}: {}", parsed_id, exc)
        message = f"Could not start the backup configure worker: {exc}"
        registry.fail(parsed_id, message)
        return _json_error(message, 503)
    return OperationHandleResponse(operation_id=str(parsed_id), kind="backup_configure"), 202


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(OperationHandleResponse, status_code=202))
def _handle_backup_service_disable(agent_id: str) -> tuple[OperationHandleResponse, int] | Response:
    """Turn a workspace's backups off; return a handle to poll.

    Archives the canonical env minds-side (old snapshots stay reachable
    through the archive) and rotates the workspace's ``restic.env`` aside so
    the backup service goes idle. Env-only -- no chat gate, and no master
    password is needed to turn backups off. The verification check will
    afterwards report NOT_CONFIGURED, which is accurate.
    """
    context = _resolve_backup_route_context(agent_id)
    if isinstance(context, Response):
        return context
    parsed_id, paths, parent_cg = context
    state = get_state()
    registry = state.workspace_operation_registry
    if not registry.start_if_idle(parsed_id, WorkspaceOperationKind.BACKUP_CONFIGURE, datetime.now(timezone.utc)):
        existing = registry.get(parsed_id)
        kind_note = f" ({existing.kind.value})" if existing is not None else ""
        return _json_error(f"Another operation{kind_note} is already running for {agent_id}", 409)
    registry.append_log(parsed_id, "Disabling backups...")
    try:
        parent_cg.start_new_thread(
            target=backup_update_module.run_backup_disable_sequence,
            kwargs={
                "agent_id": parsed_id,
                "paths": paths,
                "parent_cg": parent_cg,
                "registry": registry,
            },
            name=f"backup-disable-{parsed_id}",
            daemon=True,
            is_checked=False,
            on_failure=backup_update_module.BackupWorkerFailureHandler(
                workspace_agent_id=parsed_id, registry=registry
            ),
        )
    except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
        logger.warning("Failed to spawn backup disable worker for {}: {}", parsed_id, exc)
        message = f"Could not start the backup disable worker: {exc}"
        registry.fail(parsed_id, message)
        return _json_error(message, 503)
    return OperationHandleResponse(operation_id=str(parsed_id), kind="backup_configure"), 202


@require_api_or_cookie_auth
@API_SPEC.validate(json=BackupVerificationToggleRequest, resp=json_response_model(EmptyResponse))
def _handle_backup_verification_toggle(agent_id: str) -> EmptyResponse | Response:
    """Enable/disable backup verification (checks + badge) for one workspace."""
    parsed_id = AgentId(agent_id)
    state = get_state()
    if parsed_id not in state.backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)
    paths = state.api_v1_paths
    if paths is None:
        return _json_error("Backup management is unavailable in this configuration", 503)
    body = request.get_json(silent=True, force=True) or {}
    if "enabled" not in body:
        return _json_error("'enabled' is required", 400)
    set_backup_verification_enabled(paths, parsed_id, bool(body.get("enabled")))
    return EmptyResponse()


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(BackupOperationStatusResponse))
def _handle_backup_operation_status(operation_id: str) -> BackupOperationStatusResponse | Response:
    """Report the status of a backup update/configure operation (the id is the workspace agent id)."""
    parsed_id = AgentId(operation_id)
    record = get_state().workspace_operation_registry.get(parsed_id)
    if record is None or record.kind not in (
        WorkspaceOperationKind.BACKUP_UPDATE,
        WorkspaceOperationKind.BACKUP_CONFIGURE,
    ):
        return _json_error(f"Unknown operation {operation_id}", 404)
    blocked_chats: tuple[str, ...] = ()
    if record.error is not None and record.error.startswith(backup_update_module.BLOCKED_BY_RUNNING_CHATS_PREFIX):
        names = record.error[len(backup_update_module.BLOCKED_BY_RUNNING_CHATS_PREFIX) :]
        blocked_chats = tuple(name for name in names.split(",") if name)
    return BackupOperationStatusResponse(
        operation_id=operation_id,
        kind=record.kind.value.lower(),
        status=str(record.status),
        is_done=record.status == WorkspaceOperationStatus.DONE,
        error=record.error,
        blocked_chats=blocked_chats,
    )


@require_api_or_cookie_auth
def _handle_backup_operation_logs(operation_id: str) -> Response:
    """Drain a backup operation's in-memory registry log queue as server-sent events."""
    parsed_id = AgentId(operation_id)
    registry = get_state().workspace_operation_registry
    log_queue = registry.get_log_queue(parsed_id) if registry.get(parsed_id) is not None else None
    if log_queue is None:
        return _json_error(f"Unknown operation {operation_id}", 404)
    return make_streaming_response(
        _stream_workspace_operation_logs(log_queue), media_type="text/event-stream", headers=_SSE_HEADERS
    )


def _sse(payload: dict[str, object]) -> str:
    """Format one server-sent-event ``data:`` frame."""
    return f"data: {json.dumps(payload)}\n\n"


def _stream_create_operation_logs(log_queue: "queue.Queue[str]") -> Iterator[str]:
    """Yield SSE frames draining a create operation's in-memory log queue.

    Emits one ``{"log": ...}`` frame per line, a keepalive while idle, and a
    final ``{"done": true}`` frame when the sentinel arrives. Exits promptly if
    the desktop client is shutting down.
    """
    shutdown_event = get_state().shutdown_event
    while not shutdown_event.is_set():
        try:
            line = log_queue.get(block=True, timeout=1.0)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        if line == LOG_SENTINEL:
            yield _sse({"done": True})
            return
        yield _sse({"log": line})


def _stream_workspace_operation_logs(log_queue: "queue.Queue[str]") -> Iterator[str]:
    """Yield SSE frames draining a workspace operation's in-memory log queue.

    Serves the restart and backup update/configure log routes alike (any
    operation tracked by the workspace-operation registry). Mirrors
    :func:`_stream_create_operation_logs` but keys on the registry's
    ``OPERATION_LOG_SENTINEL`` end-of-stream marker.
    """
    shutdown_event = get_state().shutdown_event
    while not shutdown_event.is_set():
        try:
            line = log_queue.get(block=True, timeout=1.0)
        except queue.Empty:
            yield ": keepalive\n\n"
            continue
        if line == OPERATION_LOG_SENTINEL:
            yield _sse({"done": True})
            return
        yield _sse({"log": line})


def _stream_destroy_operation_logs(agent_id: AgentId, paths: WorkspacePaths) -> Iterator[str]:
    """Yield SSE frames tailing a destroy operation's on-disk log to completion.

    Polls the log file from the last offset, emitting new content as ``{"log":
    ...}`` frames, and stops once the destroy record reaches a terminal status
    (with a final ``{"done": true}`` frame). Exits promptly on shutdown.
    """
    shutdown_event = get_state().shutdown_event
    backend_resolver = get_state().backend_resolver
    offset = 0
    while not shutdown_event.is_set():
        try:
            content_bytes, offset = destroying.read_log_chunk(agent_id, paths, offset)
        except FileNotFoundError:
            content_bytes = b""
        if content_bytes:
            yield _sse({"log": content_bytes.decode("utf-8", errors="replace")})
        is_host_still_active = destroying.is_host_still_active(backend_resolver, paths, agent_id)
        record = destroying.read_destroying(agent_id, paths, is_host_still_active)
        if record is not None and record.status != destroying.DestroyingStatus.RUNNING:
            # Flush any final bytes written between the last read and termination.
            try:
                tail_bytes, offset = destroying.read_log_chunk(agent_id, paths, offset)
            except FileNotFoundError:
                tail_bytes = b""
            if tail_bytes:
                yield _sse({"log": tail_bytes.decode("utf-8", errors="replace")})
            yield _sse({"done": True, "status": str(record.status)})
            return
        yield ": keepalive\n\n"
        # Wait out the poll interval on the shutdown event (not time.sleep) so a
        # shutdown wakes us immediately and the loop stays responsive.
        shutdown_event.wait(timeout=_DESTROY_LOG_POLL_SECONDS)


@require_api_or_cookie_auth
def _handle_create_operation_logs(operation_id: str) -> Response:
    """Stream a create operation's in-memory log queue as server-sent events."""
    agent_creator: AgentCreator | None = get_state().agent_creator
    log_queue = agent_creator.get_log_queue(CreationId(operation_id)) if agent_creator is not None else None
    if log_queue is None:
        return _json_error(f"Unknown operation {operation_id}", 404)
    return make_streaming_response(
        _stream_create_operation_logs(log_queue), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@require_api_or_cookie_auth
def _handle_destroy_operation_logs(operation_id: str) -> Response:
    """Tail a destroy operation's on-disk log to completion as server-sent events."""
    parsed_id = AgentId(operation_id)
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return _json_error(f"Unknown operation {operation_id}", 404)
    is_host_still_active = destroying.is_host_still_active(get_state().backend_resolver, paths, parsed_id)
    if destroying.read_destroying(parsed_id, paths, is_host_still_active) is None:
        return _json_error(f"Unknown operation {operation_id}", 404)
    return make_streaming_response(
        _stream_destroy_operation_logs(parsed_id, paths), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@require_api_or_cookie_auth
def _handle_restart_operation_logs(operation_id: str) -> Response:
    """Drain a restart operation's in-memory registry log queue as server-sent events."""
    parsed_id = AgentId(operation_id)
    registry = get_state().workspace_operation_registry
    log_queue = registry.get_log_queue(parsed_id) if registry.get(parsed_id) is not None else None
    if log_queue is None:
        return _json_error(f"Unknown operation {operation_id}", 404)
    return make_streaming_response(
        _stream_workspace_operation_logs(log_queue), media_type="text/event-stream", headers=_SSE_HEADERS
    )


# -- SSH access route --


@require_api_or_cookie_auth
@API_SPEC.validate(json=EstablishSshRequest, resp=json_response_model(SshConnectionResponse))
def _handle_establish_ssh(agent_id: str) -> SshConnectionResponse | Response:
    """Authorize temporary SSH access into a workspace and return its connection info.

    Body: ``{"public_key": "<openssh public key>", "requester_workspace_id":
    "<caller's own id>"}``. The caller's private key never leaves the caller.
    The hub reads the target's ``authorized_keys`` back over ``mngr exec``,
    prunes any expired minds-owned grant lines, drops any still-valid grant the
    same requester already holds (so a re-request refreshes rather than stacks),
    appends the new (TTL-tagged) public key, writes the result back in one
    rewrite, and returns SSH connection info. Pruning on every grant means
    repeated requests never let stale or duplicate grant lines pile up.

    The returned endpoint depends on where the target lives. A *remote* target
    (Modal/AWS/Vultr/imbue_cloud) is reachable from anywhere, so its real
    ``user``/``host``/``port`` are returned and the caller connects directly. A
    *local* target (Docker/Lima) publishes its sshd only on the hub's own
    loopback, which a peer (or remote) workspace cannot reach, so the hub brokers
    a reverse tunnel into the *caller's* container and returns
    ``host="127.0.0.1"`` with the loopback port assigned inside that container;
    the caller connects there with the same key. The target must be online;
    reading or writing the key on a stopped target fails at the ``mngr exec``
    step (502), as does brokering a tunnel into an unreachable caller.
    """
    parsed_id = AgentId(agent_id)
    backend_resolver = get_state().backend_resolver
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return _json_error(f"Unknown workspace {agent_id}", 404)
    parent_cg = get_state().root_concurrency_group
    if parent_cg is None:
        return _json_error("SSH access not configured", 501)

    # Body shape (``public_key`` + ``requester_workspace_id`` present, strings) is
    # enforced by the spectree model; the empty-after-strip check below is semantic.
    body = request.get_json(silent=True, force=True) or {}
    public_key = str(body.get("public_key", ""))
    requester_workspace_id = str(body.get("requester_workspace_id", "")).strip()
    if not requester_workspace_id:
        return _json_error("'requester_workspace_id' is required", 400)

    # The hub must have an SSH endpoint it can reach for the target. Discovery
    # provides one for every real provider (a remote address for remote hosts; a
    # ``127.0.0.1:<published port>`` loopback for local Docker/Lima); only the
    # bare local provider, which minds workspaces never use, lacks one.
    ssh_info = backend_resolver.get_ssh_info(parsed_id)
    if ssh_info is None:
        return _json_error("Target workspace has no SSH endpoint that this desktop client can resolve", 501)

    now = datetime.now(timezone.utc)
    try:
        expires_at = now + workspace_ssh.DEFAULT_SSH_GRANT_TTL
        authorized_line = workspace_ssh.build_authorized_keys_line(
            public_key=public_key,
            requester_workspace_id=requester_workspace_id,
            expires_at=expires_at,
        )
    except workspace_ssh.SshGrantError as e:
        return _json_error(str(e), 400)

    mngr_binary = get_state().mngr_binary

    # Read the target's current authorized_keys (absent file -> empty), prune any
    # expired minds-owned grant lines, append the new grant, and write the whole
    # body back in one rewrite. Read + write are two mngr exec round-trips; the
    # prune logic lives in workspace_ssh so it stays unit-tested.
    #
    # ``mngr exec`` takes the command as a single trailing COMMAND argument
    # (its CLI is ``mngr exec [AGENTS]... COMMAND``) and runs it in a shell with
    # the agent's env sourced, so ``~`` expands and the redirection works. We
    # must NOT pass ``-- bash -c <script>``: the extra ``bash``/``-c`` tokens are
    # parsed as additional agent names (``-c`` fails agent-name validation) and
    # the whole call errors out.
    #
    # The read is captured with ``--format json`` and the command's own stdout is
    # pulled out of the structured envelope. In its default (human) format
    # ``mngr exec`` appends a ``Command succeeded on agent <name>`` status line to
    # stdout after the command's output; reading that raw would write the status
    # line straight back into the target's authorized_keys -- and, because the
    # prune step only drops minds-owned grant lines, it would accumulate another
    # copy on every re-grant. The JSON envelope keeps the captured body clean.
    read_argv = [
        mngr_binary,
        "exec",
        str(parsed_id),
        "cat ~/.ssh/authorized_keys 2>/dev/null || true",
        "--format",
        "json",
    ]
    try:
        read_returncode, read_stdout, read_stderr = _run_mngr_blocking(read_argv, parent_cg)
    except (OSError, ConcurrencyGroupError) as e:
        return _json_error(f"Could not read the target's authorized_keys: {e}", 502)
    if read_returncode != 0:
        return _json_error(f"Could not read the target's authorized_keys: {read_stderr.strip()}", 502)
    try:
        read_result = json.loads(read_stdout)
        existing_authorized_keys = str(read_result["results"][0]["stdout"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logger.warning("Could not parse the target's authorized_keys read for {}: {}", parsed_id, e)
        return _json_error(f"Could not parse the target's authorized_keys read: {e}", 502)

    new_authorized_keys = workspace_ssh.compose_pruned_authorized_keys(
        existing_authorized_keys, authorized_line, requester_workspace_id=requester_workspace_id, now=now
    )
    write_script = (
        "set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh; "
        f"printf '%s' {shlex.quote(new_authorized_keys)} > ~/.ssh/authorized_keys; "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    # Single trailing COMMAND arg (see the read above) -- mngr exec runs it in a shell.
    write_argv = [mngr_binary, "exec", str(parsed_id), write_script]
    try:
        write_returncode, _write_stdout, write_stderr = _run_mngr_blocking(write_argv, parent_cg)
    except (OSError, ConcurrencyGroupError) as e:
        return _json_error(f"Could not authorize SSH key on the target: {e}", 502)
    if write_returncode != 0:
        return _json_error(f"Could not authorize SSH key on the target: {write_stderr.strip()}", 502)

    # Decide how the caller reaches the target. A routable (remote) target is
    # connected to directly. A local target's sshd is on the hub's loopback, so
    # broker a reverse tunnel into the caller's container and hand back the
    # loopback endpoint the caller connects to instead.
    if workspace_ssh_tunnel.is_loopback_host(ssh_info.host):
        caller_ssh = backend_resolver.get_ssh_info(AgentId(requester_workspace_id))
        if caller_ssh is None:
            return _json_error(
                "Cannot broker SSH to a local target: the requesting workspace has no "
                "hub-reachable SSH endpoint (is it online and known to this desktop client?).",
                502,
            )
        try:
            broker_port = workspace_ssh_tunnel.broker_reverse_tunnel_into_caller(
                get_state().ssh_tunnel_manager,
                caller_ssh=caller_ssh,
                target_ssh=ssh_info,
                target_agent_id=str(parsed_id),
            )
        except workspace_ssh_tunnel.WorkspaceSshTunnelError as e:
            return _json_error(f"Could not broker an SSH tunnel into the requesting workspace: {e}", 502)
        connection = workspace_ssh.SshConnectionInfo(
            user=ssh_info.user, host="127.0.0.1", port=broker_port, expires_at=expires_at
        )
    else:
        connection = workspace_ssh.SshConnectionInfo(
            user=ssh_info.user, host=ssh_info.host, port=ssh_info.port, expires_at=expires_at
        )
    return SshConnectionResponse(
        agent_id=str(parsed_id),
        user=connection.user,
        host=connection.host,
        port=connection.port,
        expires_at=connection.expires_at.isoformat(),
    )


# -- Bug report route --


@require_api_or_cookie_auth
@API_SPEC.validate(json=BugReportRequest, resp=json_response_model(OkResponse))
def _handle_bug_report(agent_id: str) -> OkResponse | Response:
    """Ask the desktop app to open the report-a-bug modal pre-filled, on behalf of an in-workspace agent.

    The agent does not submit to Sentry itself: a human gates every send. This route hands the agent's
    description to the desktop app, which pops the report modal -- pre-filled with that description and
    scoped to the caller's own workspace (the path ``agent_id``, which the gateway has already
    authorized) -- in the window showing that workspace. The user then reviews, picks what to attach, and
    submits through the same ``/help/report`` path as a manual report.
    """
    # ``description`` presence/type is enforced by the spectree model; the
    # whitespace-only rejection below is value-semantic.
    body = request.get_json(silent=True, force=True) or {}
    description = str(body.get("description", "")).strip()
    if not description:
        return _json_error("'description' field is required and must be a non-empty string", 400)

    get_state().help_modal_request_broker.request_open(
        OpenHelpRequest(description=description, workspace_agent_id=agent_id)
    )
    # The agent never submits to Sentry itself, so no report event is written here (the
    # response carries no ``event_id``); the human-reviewed send flows through ``/help/report``.
    return OkResponse(ok=True)


# -- Workspace metadata update route (color + account association) --


@require_api_or_cookie_auth
@API_SPEC.validate(json=PatchWorkspaceRequest)
def _handle_patch_workspace(agent_id: str) -> Response:
    """Partially update a workspace's metadata (color and/or account association).

    JSON body may carry any of: ``color`` (a hex string, normalized + written via
    ``mngr label``); ``account_id`` (a string to associate, or ``null`` / empty
    string to disassociate). Only the keys present in the body are applied.
    Returns 200 with the applied fields (``agent_id`` plus each of ``color`` /
    ``account_id`` that was set).
    """
    parsed_id = AgentId(agent_id)
    # The spectree model validates the (all-optional) body shape; only keys present
    # in the raw body are applied, so an empty body is a no-op.
    body = request.get_json(silent=True, force=True) or {}

    state = get_state()
    backend_resolver = state.backend_resolver
    applied: dict[str, object] = {"agent_id": str(parsed_id)}

    if "color" in body:
        try:
            applied["color"] = workspace_settings.set_workspace_color(
                parsed_id,
                str(body.get("color", "")),
                backend_resolver,
                state.mngr_binary,
                state.mngr_host_dir,
                state.root_concurrency_group,
            )
        except workspace_settings.WorkspaceColorError as exc:
            return _json_error(exc.code, exc.status_code)

    if "account_id" in body:
        account_value = body.get("account_id")
        is_disassociate = account_value is None or (isinstance(account_value, str) and not account_value.strip())
        try:
            if is_disassociate:
                workspace_settings.disassociate_workspace_account(
                    parsed_id, backend_resolver, state.session_store, state.imbue_cloud_cli
                )
                applied["account_id"] = None
            else:
                account_id = str(account_value).strip()
                account = workspace_settings.associate_workspace_account(
                    parsed_id, account_id, backend_resolver, state.session_store
                )
                # Echo the *resolved* id (the input may have been an email) plus the
                # email, so the caller can confirm exactly which account was bound.
                applied["account_id"] = account.user_id
                applied["account_email"] = account.email
        except workspace_settings.WorkspaceAssociationError as exc:
            return _json_error(str(exc), exc.status_code)

    return _json_response(applied)


# -- Workspace operation dismissal --


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(EmptyResponse))
def _handle_dismiss_destroy_operation(operation_id: str) -> EmptyResponse:
    """Dismiss a finished destroy operation card (replaces ``/api/destroying/<id>/dismiss``).

    Removes the on-disk destroy record (the id is the workspace ``AgentId``).
    Idempotent: an unknown id, or a missing data dir, is a no-op. Always 200 ``{}``.
    """
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is not None:
        destroying.delete_destroying(AgentId(operation_id), paths)
    return EmptyResponse()


# -- Sharing sub-resource routes --


@require_api_or_cookie_auth
def _handle_sharing_status(agent_id: str, service_name: str) -> Response:
    """Return current sharing status for a service: ``{enabled, url, policy}``."""
    state = get_state()
    status = get_sharing_status(
        AgentId(agent_id), ServiceName(service_name), state.imbue_cloud_cli, state.session_store
    )
    return _json_response(status)


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(SharingReadinessResponse))
def _handle_sharing_readiness(agent_id: str, service_name: str) -> SharingReadinessResponse:
    """Probe a shared service's hostname to see if Cloudflare Access is live yet.

    The hostname to probe comes from the ``url`` query param; restricted to
    public ``https`` URLs to avoid an SSRF vector. Contract: ``{"ready": bool}``.
    """
    probe_url = request.args.get("url", "")
    http_client = get_state().http_client
    if http_client is None or not is_probeable_share_url(probe_url):
        return SharingReadinessResponse(ready=False)
    return SharingReadinessResponse(ready=probe_share_url_readiness(http_client, probe_url))


@require_api_or_cookie_auth
@API_SPEC.validate(json=EnableSharingRequest, resp=json_response_model(SharingToggleResponse))
def _handle_sharing_enable(agent_id: str, service_name: str) -> SharingToggleResponse | Response:
    """Enable or update sharing for a service. Body: ``{"emails": [...]}``."""
    parsed_id = AgentId(agent_id)
    # The spectree model validates that ``emails`` (when present) is a list of strings.
    body = request.get_json(silent=True, force=True) or {}
    emails = [str(email) for email in body.get("emails", [])]
    try:
        enable_sharing_via_cloudflare(
            agent_id=parsed_id,
            service_name=ServiceName(service_name),
            emails=emails,
            backend_resolver=get_state().backend_resolver,
        )
    except SharingError as exc:
        return _json_error(str(exc), 502)
    return SharingToggleResponse(agent_id=str(parsed_id), service_name=service_name, enabled=True)


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(SharingToggleResponse))
def _handle_sharing_disable(agent_id: str, service_name: str) -> SharingToggleResponse | Response:
    """Disable sharing for a service (removes it from its tunnel; the tunnel persists)."""
    state = get_state()
    try:
        disable_sharing(AgentId(agent_id), ServiceName(service_name), state.imbue_cloud_cli, state.session_store)
    except SharingError as exc:
        return _json_error(str(exc), 502)
    return SharingToggleResponse(agent_id=agent_id, service_name=service_name, enabled=False)


# -- Desktop namespace routes (cookie-or-bearer; no agent verb) --
#
# These manage install-scoped app state (provider config, host/state-container
# lifecycle). They mint no ``minds-workspaces`` verb, so agents are blocked at
# the gateway (deny-all baseline) while the desktop UI reaches them by cookie.


@require_api_or_cookie_auth
@API_SPEC.validate(json=SetProviderEnabledRequest, resp=json_response_model(ProviderToggleResponse))
def _handle_patch_provider(provider_name: str) -> ProviderToggleResponse | Response:
    """Set a provider's ``is_enabled`` flag. Body: ``{"enabled": bool}``.

    Idempotent desired-state. Refuses to disable a provider that still has active
    workspaces (409) -- disabling it would drop those live workspaces off
    discovery.
    """
    # ``enabled`` (a required bool) is validated by the spectree model.
    body = request.get_json(silent=True, force=True) or {}
    enabled = bool(body.get("enabled"))
    state = get_state()
    try:
        changed = desktop_control.set_provider_enabled(
            provider_name, enabled, state.backend_resolver, state.latchkey_forward_supervisor
        )
    except desktop_control.ProviderHasActiveWorkspacesError as exc:
        return _json_error(str(exc), 409)
    return ProviderToggleResponse(provider_name=provider_name, enabled=enabled, changed=changed)


@require_api_or_cookie_auth
def _handle_running_workspaces() -> Response:
    """Return the shutdown-capable workspaces whose containers are currently running."""
    return _json_response({"running": desktop_control.running_workspace_entries(get_state().backend_resolver)})


@require_api_or_cookie_auth
def _handle_host_name_available() -> Response:
    """Report whether a workspace name is free (``GET .../desktop/host-name-available``).

    Read-only liveness check for the create form's Name field. Reads the
    discovery snapshot (resolver cache) -- no provider/subprocess call -- and
    answers whether ``name`` is already taken by an *active* workspace on the
    provider instance the selected ``launch_mode`` / ``account_id`` / ``region``
    would target. Format validation is left to the client; an empty or malformed
    name reports available (only a valid name can collide, and the client
    surfaces its own format message). Cookie-only (desktop namespace): the
    browser create page is the sole caller.

    Query params: ``name`` (required), ``launch_mode``, ``account_id``,
    ``region``. Returns ``{"available": bool}``.
    """
    name = request.args.get("name", "").strip()
    if not name:
        return _json_response({"available": True})
    try:
        HostName(name)
    except InvalidName:
        return _json_response({"available": True})

    try:
        launch_mode = LaunchMode(str(request.args.get("launch_mode", LaunchMode.DOCKER.value)))
    except ValueError:
        launch_mode = LaunchMode.DOCKER
    account_id = request.args.get("account_id", "").strip()
    region = request.args.get("region", "").strip()

    # Imbue Cloud is per-account, so its provider instance (``imbue_cloud_<slug>``)
    # is named from the account email; the session store maps user_id -> email.
    account_email = ""
    if account_id and launch_mode is LaunchMode.IMBUE_CLOUD:
        session_store: MultiAccountSessionStore | None = get_state().session_store
        if session_store is not None:
            account_email = session_store.get_account_email(account_id) or ""

    try:
        provider_instance_name = provider_instance_name_for_launch(
            launch_mode, imbue_cloud_account=account_email or None, region=region or None
        )
    except MngrCommandError:
        # Not enough context to scope (imbue_cloud without an account, or AWS
        # without a region). The form blocks submit on those separately, so
        # report available rather than a spurious conflict.
        return _json_response({"available": True})

    taken = taken_host_names_on_provider(get_state().backend_resolver, provider_instance_name)
    return _json_response({"available": name.casefold() not in taken})


@require_api_or_cookie_auth
def _handle_stop_hosts() -> Response:
    """Stop the hosts of the requested workspaces in one ``mngr stop --stop-host``.

    The target workspace agent ids come from repeated ``agent_id`` query params.
    Returns the requested workspaces still running after the attempt.
    """
    state = get_state()
    parent_cg = state.root_concurrency_group
    if parent_cg is None:
        return _json_error("Workspace host control is unavailable in this configuration", 503)
    requested_ids = request.args.getlist("agent_id")
    still_running = desktop_control.stop_workspace_hosts(
        requested_ids, state.backend_resolver, state.mngr_binary, state.mngr_host_dir, parent_cg
    )
    return _json_response({"still_running": still_running})


@require_api_or_cookie_auth
@API_SPEC.validate(resp=json_response_model(StopStateContainerResponse))
def _handle_stop_state_container() -> StopStateContainerResponse | Response:
    """Stop this env's mngr Docker state container, to fully free local resources at quit."""
    state = get_state()
    parent_cg = state.root_concurrency_group
    if parent_cg is None:
        return StopStateContainerResponse(stopped=False)
    try:
        stopped = desktop_control.stop_state_container(state.mngr_host_dir, parent_cg)
    except DockerCleanupError as exc:
        logger.warning("Failed to stop the Docker state container at shutdown: {}", exc)
        return _json_error(f"Could not stop the Docker state container: {exc}", 500)
    return StopStateContainerResponse(stopped=stopped)


# -- Blueprint factory --


def create_api_v1_blueprint() -> Blueprint:
    """Create the /api/v1/ blueprint with all REST API endpoints."""
    blueprint = Blueprint("api_v1", __name__, url_prefix="/api/v1")

    # A malformed workspace/operation id in any route's path -> 400, not a 500.
    blueprint.register_error_handler(InvalidRandomIdError, _handle_invalid_random_id)

    # Notifications (per-agent so the gateway's per-host permission file
    # can restrict each caller to its own agent ids).
    blueprint.add_url_rule("/agents/<agent_id>/notifications", view_func=_handle_notification, methods=["POST"])

    # Cross-workspace management (read surface). Gated by the
    # ``minds-workspaces`` detent scope at the gateway.
    blueprint.add_url_rule("/workspaces", view_func=_handle_list_workspaces, methods=["GET"])
    blueprint.add_url_rule("/workspaces/<agent_id>", view_func=_handle_get_workspace, methods=["GET"])
    # Gated by the must-ask ``minds-accounts-read`` permission (not in the agent baseline).
    blueprint.add_url_rule("/accounts", view_func=_handle_list_accounts, methods=["GET"])
    blueprint.add_url_rule("/workspaces/<agent_id>/version", view_func=_handle_workspace_version, methods=["GET"])
    blueprint.add_url_rule("/workspaces/<agent_id>/backups", view_func=_handle_workspace_backups, methods=["GET"])
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/backups/<snapshot_id>/export",
        view_func=_handle_workspace_backup_export,
        methods=["POST"],
    )

    # Cross-workspace mutation (create / destroy / lifecycle) + operation polling.
    blueprint.add_url_rule("/workspaces", view_func=_handle_create_workspace, methods=["POST"])
    blueprint.add_url_rule("/workspaces/<agent_id>/destroy", view_func=_handle_destroy_workspace, methods=["POST"])
    blueprint.add_url_rule("/workspaces/<agent_id>/rename", view_func=_handle_workspace_rename, methods=["POST"])
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/start",
        view_func=_handle_workspace_start,
        endpoint="workspace_start",
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/stop",
        view_func=_handle_workspace_stop,
        endpoint="workspace_stop",
        methods=["POST"],
    )
    # Workspace recovery (health probe + restart). Gated by
    # ``minds-workspaces-recover`` at the gateway.
    blueprint.add_url_rule("/workspaces/<agent_id>/health", view_func=_handle_workspace_health, methods=["GET"])
    blueprint.add_url_rule("/workspaces/<agent_id>/restart", view_func=_handle_workspace_restart, methods=["POST"])

    # Backup service verification + management. The per-workspace health read
    # (folded into ``/workspaces/<agent_id>/backups`` above) rides the
    # ``minds-workspaces-read`` grant; the mutating backup-service routes are
    # gated by ``minds-workspaces-backups-manage`` at the gateway.
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/backup-service/update",
        view_func=_handle_backup_service_update,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/backup-service/update/cancel",
        view_func=_handle_backup_service_update_cancel,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/backup-service/configure",
        view_func=_handle_backup_service_configure,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/backup-service/disable",
        view_func=_handle_backup_service_disable,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/backup-service/verification",
        view_func=_handle_backup_verification_toggle,
        methods=["POST"],
    )

    # Operation polling is type-segmented: ``/operations/<type>/<id>`` (type in
    # create | destroy | restart | backup). The caller always knows the type, so
    # each gets a dedicated handler + precise response model (no id-prefix
    # dispatch).
    blueprint.add_url_rule(
        "/workspaces/operations/create/<operation_id>",
        view_func=_handle_create_operation_status,
        endpoint="create_operation_status",
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/operations/destroy/<operation_id>",
        view_func=_handle_destroy_operation_status,
        endpoint="destroy_operation_status",
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/operations/restart/<operation_id>",
        view_func=_handle_restart_operation_status,
        endpoint="restart_operation_status",
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/operations/create/<operation_id>/logs",
        view_func=_handle_create_operation_logs,
        endpoint="create_operation_logs",
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/operations/destroy/<operation_id>/logs",
        view_func=_handle_destroy_operation_logs,
        endpoint="destroy_operation_logs",
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/operations/restart/<operation_id>/logs",
        view_func=_handle_restart_operation_logs,
        endpoint="restart_operation_logs",
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/operations/backup/<operation_id>",
        view_func=_handle_backup_operation_status,
        endpoint="backup_operation_status",
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/operations/backup/<operation_id>/logs",
        view_func=_handle_backup_operation_logs,
        endpoint="backup_operation_logs",
        methods=["GET"],
    )

    # Workspace metadata update (color + account association). Gated by
    # ``minds-workspaces-update`` at the gateway.
    blueprint.add_url_rule(
        "/workspaces/<agent_id>",
        view_func=_handle_patch_workspace,
        endpoint="patch_workspace",
        methods=["PATCH"],
    )

    # Operation dismissal (replaces /api/destroying/<id>/dismiss). Only a destroy
    # operation has a dismissable on-disk record; create/restart cards self-clear.
    blueprint.add_url_rule(
        "/workspaces/operations/destroy/<operation_id>",
        view_func=_handle_dismiss_destroy_operation,
        endpoint="dismiss_destroy_operation",
        methods=["DELETE"],
    )

    # Sharing sub-resource. Gated by ``minds-workspaces-sharing`` at the gateway.
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/sharing/<service_name>",
        view_func=_handle_sharing_status,
        endpoint="sharing_status",
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/sharing/<service_name>/readiness",
        view_func=_handle_sharing_readiness,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/sharing/<service_name>",
        view_func=_handle_sharing_enable,
        endpoint="sharing_enable",
        methods=["PUT"],
    )
    blueprint.add_url_rule(
        "/workspaces/<agent_id>/sharing/<service_name>",
        view_func=_handle_sharing_disable,
        endpoint="sharing_disable",
        methods=["DELETE"],
    )

    # Desktop namespace (cookie-or-bearer; no agent verb, so deny-all at the gateway).
    blueprint.add_url_rule("/desktop/providers/<provider_name>", view_func=_handle_patch_provider, methods=["PATCH"])
    blueprint.add_url_rule("/desktop/running-workspaces", view_func=_handle_running_workspaces, methods=["GET"])
    blueprint.add_url_rule("/desktop/host-name-available", view_func=_handle_host_name_available, methods=["GET"])
    blueprint.add_url_rule("/desktop/stop-hosts", view_func=_handle_stop_hosts, methods=["POST"])
    blueprint.add_url_rule("/desktop/state-container/stop", view_func=_handle_stop_state_container, methods=["POST"])

    # SSH access (establish): inject a public key + return connection info.
    blueprint.add_url_rule("/workspaces/<agent_id>/ssh", view_func=_handle_establish_ssh, methods=["POST"])

    # Bug reports (per-agent for the same gateway-permission reason; the agent_id
    # also scopes the report's workspace context).
    blueprint.add_url_rule("/agents/<agent_id>/report", view_func=_handle_bug_report, methods=["POST"])

    return blueprint
