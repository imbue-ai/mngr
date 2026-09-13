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
- ``GET /ui/api/create/attempts/<create_attempt_id>`` -- the Creating page's
  detail: the live in-flight attempt, the record-backed interrupted/failed
  view, or "gone".

Some small derivations here (suggested color, locked emails, destroy statuses)
mirror private helpers in ``app.py``; importing them would be circular
(``app.py`` imports the ``/ui`` blueprint, which imports this module), so the
logic is re-derived from the same underlying modules. When the legacy SSE
surface is deleted, those helpers should collapse into one shared home.
"""

from flask import Blueprint
from flask import Response
from flask import request
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.create_status import expected_create_attempt_duration_seconds
from imbue.minds.desktop_client.destroying import is_host_still_active
from imbue.minds.desktop_client.destroying import list_destroying
from imbue.minds.desktop_client.onboarding_services import list_onboarding_services
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.region_preference import IMBUE_CLOUD_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import VULTR_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import known_regions_for_provider
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
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
from imbue.mngr_latchkey.services_catalog import ServicesCatalog

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
    selected_docker_runtime: str = Field(description="Platform-default container runtime")
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


class LandingExtrasResponse(FrozenModel):
    """Landing-page facts that do not ride the ``workspaces`` channel message."""

    destroying_status_by_agent_id: dict[str, str] = Field(description="agent id -> running | failed destroys")
    locked_account_emails: tuple[str, ...] = Field(description="Accounts with synced secrets but no local key")
    is_discovery_complete: bool = Field(description="Whether initial discovery has completed")
    has_restorable_workspaces: bool = Field(description="Whether the last-good topology knows any workspace")


class OnboardingCloudApp(FrozenModel):
    """One app in the onboarding walkthrough's app-cloud icon wheel."""

    icon: str = Field(description="The app's brand icon, inlined as a data: URI")
    name: str = Field(description="Display name")


class LiveCreateAttemptDetail(FrozenModel):
    """The Creating page's live-attempt facts (status itself is polled from /api/v1)."""

    workspace_name: str = Field(description="Display name for the header")
    provider_label: str = Field(description="Friendly compute-provider label")
    is_remote: bool = Field(description="Whether the machine runs in the cloud (drives walkthrough copy + graphics)")
    expected_duration_seconds: float = Field(description="Expected create duration for the progress bar's easing")
    onboarding_services: tuple[OnboardingCloudApp, ...] = Field(
        description="Apps for the walkthrough's app-cloud icon wheel, icons pre-inlined"
    )


class RecordCreateAttemptDetail(FrozenModel):
    """The record-backed detail for an attempt with no live thread behind it."""

    state: str = Field(description="interrupted | failed")
    workspace_name: str = Field(description="Display name for the header")
    error: str | None = Field(default=None, description="Persisted error message for failed records")
    error_kind: str | None = Field(default=None, description="Machine-readable failure classification")
    log_tail: tuple[str, ...] = Field(default=(), description="Persisted tail of the create log")
    provider_label: str = Field(default="", description="Friendly compute-provider label")


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
    )
    return _json_response(response)


def _destroying_statuses(backend_resolver: BackendResolverInterface) -> dict[str, str]:
    """Read-only run/failed status per in-flight destroy record.

    The publisher's derive tick owns finalizing DONE records; this view only
    labels what exists right now: a dead wrapper PID with the host still up is
    a failed destroy, anything else still running.
    """
    paths = get_state().api_v1_paths
    if paths is None:
        return {}
    records = list_destroying(paths, lambda agent_id: is_host_still_active(backend_resolver, paths, agent_id))
    statuses: dict[str, str] = {}
    for agent_id, record in records.items():
        if not record.is_host_still_active:
            # Host already gone: the next derive tick finalizes and drops the
            # record; "running" until then avoids a spurious failed-flash.
            statuses[str(agent_id)] = "running"
        elif record.pid_alive:
            statuses[str(agent_id)] = "running"
        else:
            statuses[str(agent_id)] = "failed"
    return statuses


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
    response = LandingExtrasResponse(
        destroying_status_by_agent_id=_destroying_statuses(backend_resolver),
        locked_account_emails=locked_emails,
        is_discovery_complete=backend_resolver.has_completed_initial_discovery(),
        has_restorable_workspaces=bool(backend_resolver.list_restorable_workspace_ids()),
    )
    return _json_response(response)


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
        display_name = ""
        if record is not None and record.request.display_name:
            display_name = record.request.display_name
        live = LiveCreateAttemptDetail(
            workspace_name=display_name or info.host_name or create_attempt_id,
            provider_label=friendly_provider_label(record.provider_instance_name if record else None),
            is_remote=info.launch_mode is LaunchMode.IMBUE_CLOUD,
            expected_duration_seconds=expected_create_attempt_duration_seconds(info.launch_mode),
            onboarding_services=tuple(
                OnboardingCloudApp(icon=service.icon_data_uri, name=service.display_name)
                for service in list_onboarding_services(ServicesCatalog())
                if service.icon_data_uri is not None
            ),
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
    )
    return _json_response(CreateAttemptDetailResponse(kind="record", record=record_detail))


def register_create_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/create/form-defaults", view_func=_handle_create_form_defaults)
    blueprint.add_url_rule("/api/create/landing-extras", view_func=_handle_landing_extras)
    blueprint.add_url_rule(
        "/api/create/attempts/<create_attempt_id>",
        view_func=_handle_create_attempt_detail,
        endpoint="ui_create_attempt_detail",
    )
