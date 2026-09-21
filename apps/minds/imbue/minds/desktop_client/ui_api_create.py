"""/ui/api routes owned by tranche T1: Landing, Create, and Creating page data.

The SPA's create surface keeps the EXISTING ``POST /api/v1/workspaces`` front
door for submissions (it is the documented single create entry point for both
agents and the browser, returning the operation id the Creating page polls).
This module serves the page DATA that used to arrive embedded in server-side
renders:

- ``GET /ui/api/create/form-defaults`` -- everything the create form needs
  (accounts, providers, regions, machine sizes, BYOK accounts, suggested
  color), plus the ``?retry=<create_attempt_id>`` pre-fill from a pending
  record.
- ``GET /ui/api/create/landing-extras`` -- landing-page facts that do not ride
  the ``workspaces`` channel message (destroy run/failed statuses,
  locked-account emails for the sync-unlock banner, and the
  discovery-completeness flag driving the empty-state choice).
- ``GET /ui/api/create/attempts/<create_attempt_id>`` -- the creation page's
  detail: the live in-flight attempt or the record-backed interrupted/failed
  view (both carrying the attempt's persisted request so the page can restate
  the chosen settings after a reload), or "gone".
- ``POST /ui/api/create/attempts/<create_attempt_id>/welcome-chat`` -- once the
  attempt is done, hands the onboarding conversation to the new workspace as
  its first chat (``welcome_chat.py``), answering the chat's id.

Some small derivations here (suggested color, locked emails, destroy statuses)
mirror private helpers in ``app.py``; importing them would be circular
(``app.py`` imports the ``/ui`` blueprint, which imports this module), so the
logic is re-derived from the same underlying modules. When the legacy SSE
surface is deleted, those helpers should collapse into one shared home.
"""

import platform
from collections.abc import Mapping

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.create_status import expected_create_attempt_duration_seconds
from imbue.minds.desktop_client.destroying import DestroyingRecord
from imbue.minds.desktop_client.destroying import DestroyingStatus
from imbue.minds.desktop_client.destroying import is_host_still_active
from imbue.minds.desktop_client.destroying import list_destroying
from imbue.minds.desktop_client.local_prerequisites import LocalBackendPrerequisite
from imbue.minds.desktop_client.local_prerequisites import host_platform_from_system
from imbue.minds.desktop_client.local_prerequisites import local_launch_mode_for
from imbue.minds.desktop_client.local_prerequisites import probe_local_prerequisites
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.region_preference import IMBUE_CLOUD_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import VULTR_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import known_regions_for_provider
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.welcome_chat import WelcomeChatRequest
from imbue.minds.desktop_client.welcome_chat import seed_welcome_chat
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import pick_unused_create_color
from imbue.minds.desktop_client.workspace_create import default_region_for_provider_with_config
from imbue.minds.desktop_client.workspace_defaults import default_workspace_git_url
from imbue.minds.desktop_client.workspace_defaults import default_workspace_template_ref
from imbue.minds.mngr_settings.byok_accounts import is_bring_your_own_cloud_enabled
from imbue.minds.mngr_settings.byok_accounts import list_cloud_account_providers
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CONFIGURED_AWS_INSTANCE_TYPES
from imbue.minds.primitives import CONFIGURED_AWS_REGIONS
from imbue.minds.primitives import CONFIGURED_AZURE_REGIONS
from imbue.minds.primitives import CONFIGURED_AZURE_VM_SIZES
from imbue.minds.primitives import CONFIGURED_GCP_MACHINE_TYPES
from imbue.minds.primitives import CONFIGURED_GCP_ZONES
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import DEFAULT_AWS_INSTANCE_TYPE
from imbue.minds.primitives import DEFAULT_AWS_REGION
from imbue.minds.primitives import DEFAULT_AZURE_REGION
from imbue.minds.primitives import DEFAULT_AZURE_VM_SIZE
from imbue.minds.primitives import DEFAULT_GCP_MACHINE_TYPE
from imbue.minds.primitives import DEFAULT_GCP_ZONE
from imbue.minds.primitives import DockerRuntime
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import default_docker_runtime
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.mngr.primitives import AgentId

# The cloud modes are bring-your-own-key-account only; they never appear as
# plain compute options in the form (each configured account is its own row).
_BYOK_ONLY_LAUNCH_MODES: frozenset[str] = frozenset({"AWS", "GCP", "AZURE"})


class CreateAccountOption(FrozenModel):
    """One signed-in account the create form can associate a workspace with."""

    user_id: str = Field(description="Account user id (the form's account_id value)")
    email: str = Field(description="Display email")


class CloudAccountOption(FrozenModel):
    """One configured bring-your-own-key cloud account."""

    name: str = Field(description="Provider block name (the create request's cloud_account value)")
    alias: str = Field(description="User-chosen display alias")
    backend: str = Field(description="Cloud backend (aws / gcp / azure)")
    region: str = Field(description="Pinned region / zone for this account entry")


class CreateRetryPrefill(FrozenModel):
    """Form pre-fill from an interrupted / failed create attempt's pending record."""

    git_url: str = Field(description="Repository URL")
    branch: str = Field(description="Branch")
    host_name: str = Field(description="Display name (or host-name slug) to pre-fill the Name field")
    launch_mode: str = Field(description="Compute launch mode value")
    docker_runtime: str = Field(description="Container runtime value")
    backup_provider: str = Field(description="Backup provider value")
    backup_api_key_env: str = Field(description="restic env block for the manual backup provider")
    account_id: str = Field(description="Associated account id, empty for private")
    region: str = Field(description="Chosen region, empty when not applicable")
    cloud_account: str = Field(description="BYOK account name; empty unless the retry targeted one that still exists")
    instance_type: str = Field(description="Chosen machine size, empty when not applicable")
    color: str = Field(description="Accent color of the interrupted attempt")


class CreateFormDefaultsResponse(FrozenModel):
    """Everything the create form needs to render (the SSR context, as JSON)."""

    accounts: tuple[CreateAccountOption, ...] = Field(description="Signed-in accounts")
    default_account_id: str = Field(description="Pre-selected account id, empty for none")
    launch_modes: tuple[str, ...] = Field(description="Selectable compute modes (BYOK-only modes excluded)")
    selected_launch_mode: str = Field(description="Pre-selected compute mode")
    docker_runtimes: tuple[str, ...] = Field(description="Container runtime options for the local Docker provider")
    selected_docker_runtime: str = Field(
        description="Pre-selected container runtime (runc unless MINDS_DOCKER_RUNTIME_DEFAULT overrides it)"
    )
    backup_providers: tuple[str, ...] = Field(description="Backup provider options")
    selected_backup_provider: str = Field(description="Pre-selected backup provider")
    region_options_by_launch_mode: dict[str, tuple[str, ...]] = Field(
        description="Region choices per compute mode (BYOK backends merged in)"
    )
    region_selected_by_launch_mode: dict[str, str] = Field(description="Pre-selected region per compute mode")
    instance_types_by_backend: dict[str, tuple[tuple[str, str], ...]] = Field(
        description="Machine-size (value, label) pairs per cloud backend"
    )
    default_instance_type_by_backend: dict[str, str] = Field(description="Default machine size per cloud backend")
    cloud_accounts: tuple[CloudAccountOption, ...] = Field(description="Configured BYOK accounts")
    byok_clouds_enabled: bool = Field(description="Whether the BYOK cloud-accounts feature is enabled")
    git_url: str = Field(description="Default template repository the form is seeded with")
    branch: str = Field(description="Default template ref paired with the default repository")
    color: str = Field(description="Suggested accent color for the new workspace")
    prefill: CreateRetryPrefill | None = Field(default=None, description="Retry pre-fill, when ?retry named a record")
    local_prerequisites: tuple[LocalBackendPrerequisite, ...] = Field(
        description="Per local backend: whether this machine can run it, and the command that installs what is missing"
    )
    local_launch_mode: str = Field(
        description="The compute mode the local preset selects: the first local backend that is ready here"
    )


class OrphanedFailedDestroy(FrozenModel):
    """A failed destroy whose workspace no longer has a row of its own on the landing list."""

    agent_id: str = Field(description="The workspace agent id the destroy was for")
    name: str = Field(description="The workspace's display name")
    accent: str = Field(description="The workspace's accent color hex")


class LandingExtrasResponse(FrozenModel):
    """Landing-page facts that do not ride the ``workspaces`` channel message."""

    destroying_status_by_agent_id: dict[str, str] = Field(description="agent id -> running | failed destroys")
    orphaned_failed_destroys: tuple[OrphanedFailedDestroy, ...] = Field(
        description="Failed destroys whose host is already gone, so no live row carries their status"
    )
    locked_account_emails: tuple[str, ...] = Field(description="Accounts with synced secrets but no local key")
    is_discovery_complete: bool = Field(description="Whether initial discovery has completed")
    has_restorable_workspaces: bool = Field(description="Whether the last-good topology knows any workspace")


class CreateAttemptRequestSummary(FrozenModel):
    """The settings a create attempt was submitted with, as the creation page restates them."""

    display_name: str = Field(description="Human-readable workspace name")
    launch_mode: str = Field(description="Compute launch mode value")
    cloud_account: str = Field(description="Bring-your-own-key account block name, empty for a plain mode")
    backup_provider: str = Field(description="Backup provider value")
    region: str = Field(description="Region, empty when the mode has none")
    instance_type: str = Field(description="Machine size, empty when the mode has none")
    repository: str = Field(description="Template repository URL or local path")
    branch: str = Field(description="Requested branch/tag, empty for the repo default")


class LiveCreateAttemptDetail(FrozenModel):
    """The creation page's live-attempt facts (status itself is polled from /api/v1)."""

    workspace_name: str = Field(description="Display name for the header")
    provider_label: str = Field(description="Friendly compute-provider label")
    is_remote: bool = Field(description="Whether the machine runs in the cloud")
    expected_duration_seconds: float = Field(description="Expected create duration for the progress bar's easing")
    request: CreateAttemptRequestSummary = Field(description="The settings the attempt was submitted with")


class RecordCreateAttemptDetail(FrozenModel):
    """The record-backed detail for an attempt with no live thread behind it."""

    state: str = Field(description="interrupted | failed")
    workspace_name: str = Field(description="Display name for the header")
    error: str | None = Field(default=None, description="Persisted error message for failed records")
    error_kind: str | None = Field(default=None, description="Machine-readable failure classification")
    log_tail: tuple[str, ...] = Field(default=(), description="Persisted tail of the create log")
    provider_label: str = Field(default="", description="Friendly compute-provider label")
    request: CreateAttemptRequestSummary = Field(description="The settings the attempt was submitted with")


class WelcomeChatResponse(FrozenModel):
    """Response for POST /ui/api/create/attempts/<id>/welcome-chat: the seeded chat's id."""

    chat_id: str = Field(description="The id of the chat the conversation continues in")


class CreateAttemptDetailResponse(FrozenModel):
    """What the /creating/<id> page should show."""

    kind: str = Field(description="live | record | gone")
    live: LiveCreateAttemptDetail | None = Field(default=None, description="Set when kind is live")
    record: RecordCreateAttemptDetail | None = Field(default=None, description="Set when kind is record")


def _unauthenticated_response() -> Response:
    return Response('{"error": "authentication required"}', status=401, mimetype="application/json")


def _json_response(model: FrozenModel) -> Response:
    return Response(model.model_dump_json(), mimetype="application/json")


def _suggested_create_color(backend_resolver: BackendResolverInterface) -> str:
    """First unused palette entry, counting label-less workspaces as the default color."""
    used = set()
    for agent_id in backend_resolver.list_active_workspace_ids():
        stored = backend_resolver.get_workspace_color(agent_id)
        used.add(stored if stored is not None else DEFAULT_WORKSPACE_COLOR)
    return pick_unused_create_color(used)


def _region_form_context() -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Region options + pre-selected default per compute mode, BYOK backends merged in."""
    state = get_state()
    options: dict[str, tuple[str, ...]] = {}
    selected: dict[str, str] = {}
    for launch_mode, provider_key in (
        (LaunchMode.IMBUE_CLOUD, IMBUE_CLOUD_PROVIDER_KEY),
        (LaunchMode.VULTR, VULTR_PROVIDER_KEY),
    ):
        options[launch_mode.value] = tuple(known_regions_for_provider(provider_key))
        selected[launch_mode.value] = default_region_for_provider_with_config(
            provider_key, state.minds_config, state.geo_location_cache
        )
    options["AWS"] = tuple(CONFIGURED_AWS_REGIONS)
    selected["AWS"] = DEFAULT_AWS_REGION
    options["GCP"] = tuple(CONFIGURED_GCP_ZONES)
    selected["GCP"] = DEFAULT_GCP_ZONE
    options["AZURE"] = tuple(CONFIGURED_AZURE_REGIONS)
    selected["AZURE"] = DEFAULT_AZURE_REGION
    return options, selected


def _read_pending_record(create_attempt_id: str) -> PendingCreateAttemptRecord | None:
    if not create_attempt_id:
        return None
    agent_creator = get_state().agent_creator
    store = agent_creator.pending_create_attempt_store if agent_creator is not None else None
    return store.read_record(create_attempt_id) if store is not None else None


def _read_retry_prefill(
    retry_create_attempt_id: str, cloud_accounts: tuple[CloudAccountOption, ...]
) -> CreateRetryPrefill | None:
    """The ``?retry=<id>`` pre-fill from a pending record, when usable (not DONE)."""
    record = _read_pending_record(retry_create_attempt_id)
    if record is None or record.state is PendingCreateAttemptState.DONE:
        return None
    retry_request = record.request
    retained_cloud_account = (
        retry_request.cloud_account
        if any(account.name == retry_request.cloud_account for account in cloud_accounts)
        else ""
    )
    return CreateRetryPrefill(
        git_url=retry_request.repo_source,
        branch=retry_request.branch,
        host_name=retry_request.display_name or retry_request.host_name,
        launch_mode=retry_request.launch_mode.value,
        docker_runtime=retry_request.docker_runtime.value,
        backup_provider=retry_request.backup_provider.value,
        backup_api_key_env=retry_request.backup_api_key_env,
        account_id=retry_request.account_id or "",
        region=retry_request.region or "",
        cloud_account=retained_cloud_account,
        instance_type=retry_request.instance_type or "",
        color=retry_request.color or DEFAULT_WORKSPACE_COLOR,
    )


def _handle_create_form_defaults() -> Response:
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    state = get_state()
    session_store = state.session_store
    accounts = tuple(
        CreateAccountOption(user_id=str(account.user_id), email=str(account.email))
        for account in (session_store.list_accounts() if session_store is not None else [])
    )
    minds_config = state.minds_config
    default_account_id = (minds_config.get_default_account_id() if minds_config is not None else None) or ""
    region_options, region_selected = _region_form_context()
    cloud_accounts = tuple(
        CloudAccountOption(name=account.name, alias=account.alias, backend=account.backend, region=account.region)
        for account in list_cloud_account_providers(root=MindsRoot.from_environment())
    )
    # The probe is injected so tests describe a machine rather than run docker;
    # an app built without one (a minimal test app, with no concurrency group
    # to run probes under) reports nothing rather than reaching for docker.
    if state.host_probe is not None:
        host_platform = host_platform_from_system(state.host_probe.platform_system())
        local_prerequisites = probe_local_prerequisites(state.host_probe)
    else:
        host_platform = host_platform_from_system(platform.system())
        local_prerequisites = ()
    response = CreateFormDefaultsResponse(
        accounts=accounts,
        default_account_id=default_account_id,
        launch_modes=tuple(mode.value for mode in LaunchMode if mode.value not in _BYOK_ONLY_LAUNCH_MODES),
        selected_launch_mode=LaunchMode.IMBUE_CLOUD.value,
        docker_runtimes=tuple(runtime.value for runtime in DockerRuntime),
        selected_docker_runtime=default_docker_runtime().value,
        backup_providers=tuple(provider.value for provider in BackupProvider),
        selected_backup_provider=BackupProvider.IMBUE_CLOUD.value,
        region_options_by_launch_mode=region_options,
        region_selected_by_launch_mode=region_selected,
        instance_types_by_backend={
            "AWS": tuple(CONFIGURED_AWS_INSTANCE_TYPES),
            "GCP": tuple(CONFIGURED_GCP_MACHINE_TYPES),
            "AZURE": tuple(CONFIGURED_AZURE_VM_SIZES),
        },
        default_instance_type_by_backend={
            "AWS": DEFAULT_AWS_INSTANCE_TYPE,
            "GCP": DEFAULT_GCP_MACHINE_TYPE,
            "AZURE": DEFAULT_AZURE_VM_SIZE,
        },
        cloud_accounts=cloud_accounts,
        byok_clouds_enabled=is_bring_your_own_cloud_enabled(),
        git_url=default_workspace_git_url(),
        branch=default_workspace_template_ref(),
        color=_suggested_create_color(state.backend_resolver),
        prefill=_read_retry_prefill(request.args.get("retry", ""), cloud_accounts),
        local_prerequisites=local_prerequisites,
        local_launch_mode=local_launch_mode_for(host_platform, local_prerequisites).value,
    )
    return _json_response(response)


def _destroying_statuses(records: Mapping[AgentId, DestroyingRecord]) -> dict[str, str]:
    """Read-only run/failed status per in-flight destroy record.

    The publisher's derive tick owns finalizing DONE records; this view only
    labels what exists right now. A DONE record reads as running until that
    tick finalizes and drops it, which avoids a spurious failed-flash.
    """
    return {
        str(agent_id): "failed" if record.status == DestroyingStatus.FAILED else "running"
        for agent_id, record in records.items()
    }


def _orphaned_failed_destroys(
    records: Mapping[AgentId, DestroyingRecord],
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
) -> tuple[OrphanedFailedDestroy, ...]:
    """The failed destroys whose workspace is no longer active, named from discovery or the synced record.

    The record may already be tombstoned (this device retires a record once
    discovery stops listing its host), and still carries the name and color.

    A destroy can fail after its host already reads DESTROYED (a non-zero exit
    during cleanup); the landing list drops such a workspace, so without this
    the failure would have nowhere to show.
    """
    active_ids = set(backend_resolver.list_active_workspace_ids())
    orphans: list[OrphanedFailedDestroy] = []
    for agent_id, record in records.items():
        if record.status != DestroyingStatus.FAILED or agent_id in active_ids:
            continue
        found = (
            session_store.record_store.find_record_any_state(str(agent_id))
            if session_store is not None and session_store.record_store is not None
            else None
        )
        synced_record = found[1] if found is not None else None
        name = backend_resolver.get_workspace_name(agent_id) or (
            synced_record.display_name if synced_record is not None else ""
        )
        accent = backend_resolver.get_workspace_color(agent_id) or (
            synced_record.color if synced_record is not None else None
        )
        orphans.append(
            OrphanedFailedDestroy(
                agent_id=str(agent_id),
                name=name or str(agent_id),
                accent=accent or DEFAULT_WORKSPACE_COLOR,
            )
        )
    return tuple(orphans)


def _handle_landing_extras() -> Response:
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    state = get_state()
    backend_resolver = state.backend_resolver
    session_store = state.session_store
    locked_emails: tuple[str, ...] = ()
    if session_store is not None and session_store.record_store is not None and state.api_v1_paths is not None:
        accounts = session_store.list_accounts()
        locked_user_ids = set(
            session_store.record_store.locked_account_user_ids([str(account.user_id) for account in accounts])
        )
        locked_emails = tuple(str(account.email) for account in accounts if str(account.user_id) in locked_user_ids)
    paths = state.api_v1_paths
    records = (
        list_destroying(paths, lambda agent_id: is_host_still_active(backend_resolver, paths, agent_id))
        if paths is not None
        else {}
    )
    response = LandingExtrasResponse(
        destroying_status_by_agent_id=_destroying_statuses(records),
        orphaned_failed_destroys=_orphaned_failed_destroys(records, backend_resolver, session_store),
        locked_account_emails=locked_emails,
        is_discovery_complete=backend_resolver.has_completed_initial_discovery(),
        has_restorable_workspaces=bool(backend_resolver.list_restorable_workspace_ids()),
    )
    return _json_response(response)


def _request_summary(request_record: PendingCreateAttemptRequest) -> CreateAttemptRequestSummary:
    return CreateAttemptRequestSummary(
        display_name=request_record.display_name or request_record.host_name,
        launch_mode=request_record.launch_mode.value,
        cloud_account=request_record.cloud_account,
        backup_provider=request_record.backup_provider.value,
        region=request_record.region,
        instance_type=request_record.instance_type,
        repository=request_record.repo_source,
        branch=request_record.branch,
    )


def _handle_create_attempt_detail(create_attempt_id: str) -> Response:
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    agent_creator = get_state().agent_creator
    if agent_creator is None:
        return _json_response(CreateAttemptDetailResponse(kind="gone"))
    try:
        parsed_id = CreateAttemptId(create_attempt_id)
    except InvalidRandomIdError:
        return _json_response(CreateAttemptDetailResponse(kind="gone"))
    info = agent_creator.get_create_attempt_info(parsed_id)
    record = _read_pending_record(create_attempt_id)
    if info is not None:
        # The record is written before the create subprocess is spawned, so a
        # live attempt normally has one; a creator without a store (minimal
        # tests) falls back to the facts the live info carries.
        request_summary = (
            _request_summary(record.request)
            if record is not None
            else CreateAttemptRequestSummary(
                display_name=info.host_name or create_attempt_id,
                launch_mode=info.launch_mode.value,
                cloud_account="",
                backup_provider="",
                region="",
                instance_type="",
                repository="",
                branch="",
            )
        )
        live = LiveCreateAttemptDetail(
            workspace_name=request_summary.display_name,
            provider_label=friendly_provider_label(record.provider_instance_name if record else None),
            is_remote=info.launch_mode is LaunchMode.IMBUE_CLOUD,
            expected_duration_seconds=expected_create_attempt_duration_seconds(info.launch_mode),
            request=request_summary,
        )
        return _json_response(CreateAttemptDetailResponse(kind="live", live=live))
    if record is None or record.state is PendingCreateAttemptState.DONE:
        return _json_response(CreateAttemptDetailResponse(kind="gone"))
    record_detail = RecordCreateAttemptDetail(
        state="failed" if record.state is PendingCreateAttemptState.FAILED else "interrupted",
        workspace_name=record.request.display_name or record.request.host_name,
        error=record.error,
        error_kind=record.error_kind,
        log_tail=record.log_tail,
        provider_label=friendly_provider_label(record.provider_instance_name or None),
        request=_request_summary(record.request),
    )
    return _json_response(CreateAttemptDetailResponse(kind="record", record=record_detail))


def _done_workspace_agent_id(create_attempt_id: str) -> AgentId | None:
    """The agent id of a finished attempt's workspace: from the live thread, else its DONE record; None until then."""
    agent_creator = get_state().agent_creator
    try:
        parsed_id = CreateAttemptId(create_attempt_id)
    except InvalidRandomIdError:
        return None
    info = agent_creator.get_create_attempt_info(parsed_id) if agent_creator is not None else None
    if info is not None and info.status is AgentCreateAttemptStatus.DONE and info.agent_id is not None:
        return info.agent_id
    record = _read_pending_record(create_attempt_id)
    if record is not None and record.state is PendingCreateAttemptState.DONE and record.agent_id:
        return AgentId(record.agent_id)
    return None


def _handle_seed_welcome_chat(create_attempt_id: str) -> Response:
    """Hand the onboarding conversation to the finished attempt's workspace as its first chat.

    The body is a :class:`WelcomeChatRequest`. Answers 200 with the chat's id; 409 while the
    attempt has no workspace yet (the page posts after DONE, so this is a race or a stale
    tab), and 502 with the script's own words when the workspace refused or could not be
    reached, in which case the page enters the workspace without the chat.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_json_error_response("Invalid JSON body", 400)
    try:
        welcome_chat = WelcomeChatRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed welcome-chat body: {}", e)
        return make_json_error_response("Invalid JSON body", 400)
    workspace_agent_id = _done_workspace_agent_id(create_attempt_id)
    if workspace_agent_id is None:
        return make_json_error_response("The workspace is not ready yet", 409)
    mngr_caller = get_state().mngr_caller or get_default_mngr_caller()
    outcome = seed_welcome_chat(mngr_caller, workspace_agent_id, welcome_chat)
    if not outcome.is_seeded:
        return make_json_error_response(
            "Couldn't open the welcome chat in the workspace.", 502, detail=outcome.failure_detail
        )
    return _json_response(WelcomeChatResponse(chat_id=outcome.chat_id))


def register_create_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule(
        "/api/create/attempts/<create_attempt_id>/welcome-chat",
        view_func=_handle_seed_welcome_chat,
        methods=["POST"],
    )
    blueprint.add_url_rule("/api/create/form-defaults", view_func=_handle_create_form_defaults)
    blueprint.add_url_rule("/api/create/landing-extras", view_func=_handle_landing_extras)
    blueprint.add_url_rule(
        "/api/create/attempts/<create_attempt_id>",
        view_func=_handle_create_attempt_detail,
        endpoint="ui_create_attempt_detail",
    )
