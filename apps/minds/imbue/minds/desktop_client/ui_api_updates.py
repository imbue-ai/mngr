"""The SPA's update surface: the channel payload plus the /ui/api/updates routes.

Single-workspace dispatch needs only ``is_update_dispatchable`` (an unreadable
version is fine: ``/update-self`` reads the workspace's own upstream). Bulk
actions claim "update everything that is behind", so they cover only
``is_update_offered``.
"""

import json
import re
import threading
from collections.abc import Callable
from typing import Final

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import StrictInt
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.kernel_fix import FORCEABLE_KERNEL_FIX_OUTCOMES
from imbue.minds.desktop_client.kernel_fix import KERNEL_FIX_REFUSAL_MESSAGE_BY_OUTCOME
from imbue.minds.desktop_client.kernel_fix import KernelFixDispatchOutcome
from imbue.minds.desktop_client.minds_config import WorkspaceAutoUpdateMode
from imbue.minds.desktop_client.minds_config import WorkspaceAutoUpdateOverride
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.settings.resolve import SettingsResolutionContext
from imbue.minds.desktop_client.settings.resolve import resolve_auto_update_workspaces
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_models import UiWorkspaceAutoUpdateOverride
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdatesMessage
from imbue.minds.desktop_client.update_service import DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME
from imbue.minds.desktop_client.update_service import SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME
from imbue.minds.desktop_client.update_service import UpdateDispatch
from imbue.minds.desktop_client.update_service import UpdateDispatchOutcome
from imbue.minds.desktop_client.update_service import UpdateSchedule
from imbue.minds.desktop_client.update_service import UpdateScheduleOutcome
from imbue.minds.desktop_client.update_service import WorkspaceUpdateService
from imbue.minds.desktop_client.update_status import EMPTY_UPDATE_WINDOW_MESSAGE
from imbue.minds.desktop_client.update_status import format_update_window
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceFactsCache
from imbue.minds.errors import MindError
from imbue.minds.errors import MindsConfigError
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId

# 409 for a workspace that cannot run the update as it stands (the user can fix that), 502 for
# one we could not reach.
_DISPATCH_STATUS_BY_OUTCOME: Final[dict[UpdateDispatchOutcome, int]] = {
    UpdateDispatchOutcome.DISPATCHED: 200,
    UpdateDispatchOutcome.ALREADY_RUNNING: 409,
    UpdateDispatchOutcome.KERNEL_RESTART_IN_FLIGHT: 409,
    UpdateDispatchOutcome.NEEDS_SIGN_IN: 409,
    UpdateDispatchOutcome.UNSUPPORTED: 409,
    UpdateDispatchOutcome.START_FAILED: 502,
    UpdateDispatchOutcome.UNREACHABLE: 502,
    UpdateDispatchOutcome.SPAWN_FAILED: 502,
    UpdateDispatchOutcome.UNCONFIRMED: 502,
}

_SCHEDULE_STATUS_BY_OUTCOME: Final[dict[UpdateScheduleOutcome, int]] = {
    UpdateScheduleOutcome.ARMED: 200,
    UpdateScheduleOutcome.NEEDS_SIGN_IN: 409,
    UpdateScheduleOutcome.NO_CRON: 409,
    UpdateScheduleOutcome.UNSUPPORTED: 409,
    UpdateScheduleOutcome.NOT_RUNNING: 409,
    UpdateScheduleOutcome.UNREACHABLE: 502,
    UpdateScheduleOutcome.FAILED: 502,
}

_CANCEL_FAILURE_MESSAGE: Final[str] = (
    "Couldn't reach this workspace to cancel its scheduled update. Make sure it's running, then try again."
)
_SKIP_FAILURE_MESSAGE: Final[str] = (
    "Couldn't reach this workspace to skip its update. Make sure it's running, then try again."
)
_SIGN_IN_FAILURE_MESSAGE: Final[str] = (
    "Couldn't open a chat in this workspace to sign in. Try again. If this persists, report a bug."
)
_DECISION_PENDING_REFUSAL: Final[str] = "This workspace's update is waiting on your decision in its chat."

# The ``code`` each refusal with a sentence of its own carries, so the SPA shows that sentence rather than its
# fallback, and knows whether it asks for a bug report. A dispatch or schedule refusal carries its outcome.
_DECISION_PENDING_CODE: Final[str] = "DECISION_PENDING"
_INVALID_REF_CODE: Final[str] = "INVALID_REF"
_NOTHING_TO_UPDATE_CODE: Final[str] = "NOTHING_TO_UPDATE"
_CANCEL_UNREACHED_CODE: Final[str] = "CANCEL_UNREACHED"
_SKIP_UNREACHED_CODE: Final[str] = "SKIP_UNREACHED"
_SIGN_IN_CHAT_FAILED_CODE: Final[str] = "SIGN_IN_CHAT_FAILED"
_EMPTY_WINDOW_CODE: Final[str] = "EMPTY_WINDOW"


# Refuses anything that could read as a flag or shell text in the chat's seed
# prompt; whether the ref exists is for the run to decide.
_OVERRIDE_REF_RE: Final[re.Pattern[str]] = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]{0,199}")


class UiUpdateTargetRequest(FrozenModel):
    """Body of an update-now or schedule write; an empty body means the skill's default target."""

    target_ref: str = Field(
        default="", description="Exact ref the run should target, '' for the newest supported release"
    )


class UiBulkUpdateRequest(FrozenModel):
    """Body of a bulk now/schedule action: which workspaces it covers."""

    agent_ids: tuple[str, ...] = Field(description="Workspaces the action covers")


def build_workspace_updates_message(
    service: WorkspaceUpdateService,
    update_window: tuple[int, int],
    settings_context: SettingsResolutionContext,
    # The per-workspace skipped targets are facts about one workspace, not declared settings:
    # they live in the workspace itself, served from the install's cache while it is stopped.
    facts_cache: WorkspaceFactsCache | None,
) -> UiWorkspaceUpdatesMessage:
    """The store's composed state, plus the per-frame facts it does not hold."""
    ceiling_ref = service.settings_source.read_ceiling()
    is_auto_update_enabled = resolve_auto_update_workspaces(settings_context)
    updates: dict[str, UiWorkspaceUpdate] = {}
    for agent_id_str, state in service.state_store.snapshot().items():
        # Each row's values are the workspace's own: its setting over its owning
        # account's, as the sweep that arms it reads them.
        effective = service.settings_source.read_effective_auto_update(AgentId(agent_id_str))
        updates[agent_id_str] = state.model_copy_update(
            # The SPA's no-backups confirmation keys on this, so the server never refuses.
            to_update(state.field_ref().is_backup_configured, service.is_backup_configured(AgentId(agent_id_str))),
            to_update(
                state.field_ref().is_update_skipped,
                facts_cache is not None and facts_cache.get_skipped_target_ref(AgentId(agent_id_str)) == ceiling_ref,
            ),
            to_update(state.field_ref().is_auto_update_enabled, effective.is_enabled),
            to_update(state.field_ref().update_window, format_update_window(effective.window)),
            to_update(state.field_ref().auto_update_override, _ui_override(effective.override)),
        )
    return UiWorkspaceUpdatesMessage(
        updates=updates,
        update_window=format_update_window(update_window),
        update_window_start_hour=update_window[0],
        update_window_end_hour=update_window[1],
        is_auto_update_enabled=is_auto_update_enabled,
    )


def _ui_override(override: WorkspaceAutoUpdateOverride | None) -> UiWorkspaceAutoUpdateOverride | None:
    if override is None:
        return None
    return UiWorkspaceAutoUpdateOverride(
        mode=override.mode,
        window_start_hour=override.window[0] if override.window is not None else None,
        window_end_hour=override.window[1] if override.window is not None else None,
    )


class UiAutoUpdateOverrideRequest(FrozenModel):
    """Body of a workspace auto-update write: its own setting, or null mode to follow the app's."""

    mode: WorkspaceAutoUpdateMode | None = Field(
        description="Auto-update in its own window, or never; null to follow the app-wide setting"
    )
    window_start_hour: StrictInt | None = Field(default=None, description="Local hour a custom window opens at")
    window_end_hour: StrictInt | None = Field(default=None, description="Local hour a custom window closes at")


def _json_response(payload: dict[str, object], status_code: int = 200) -> Response:
    return make_response(status_code=status_code, content=json.dumps(payload), media_type="application/json")


def _error_response(message: str, status_code: int, detail: str = "", code: str = "") -> Response:
    """An error body; ``detail`` is what the machine, mngr, or the host start said, shown apart from the message.

    ``code`` names a dispatch or schedule outcome for a surface that offers that outcome's remedy.
    """
    payload: dict[str, object] = {"error": message}
    if detail:
        payload["detail"] = detail
    if code:
        payload["code"] = code
    return _json_response(payload, status_code)


def _resolve_service() -> WorkspaceUpdateService | None:
    return get_state().workspace_update_service


def _parse_agent_id(agent_id: str) -> AgentId | None:
    try:
        return AgentId(agent_id)
    except ValueError:
        return None


def _dispatch_response(dispatch: UpdateDispatch) -> Response:
    status = _DISPATCH_STATUS_BY_OUTCOME[dispatch.outcome]
    if status == 200:
        return _json_response({"ok": True})
    return _error_response(
        DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[dispatch.outcome], status, dispatch.failure_detail, dispatch.outcome.value
    )


def _armed_target_ref(service: WorkspaceUpdateService, agent_id: AgentId) -> str:
    """The version this machine's armed schedule names, or '' when nothing is armed or it names none.

    The only way to clear one is to cancel the schedule: both version-field buttons refuse an empty
    field, so an empty request never means "put this machine back on the default target".
    """
    return service.state_store.get(agent_id).override_ref


def _schedule_response(schedule: UpdateSchedule) -> Response:
    status = _SCHEDULE_STATUS_BY_OUTCOME[schedule.outcome]
    if status == 200:
        return _json_response({"ok": True})
    return _error_response(
        SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME[schedule.outcome], status, schedule.failure_detail, schedule.outcome.value
    )


def _resolve_machine_request(agent_id: str) -> tuple[WorkspaceUpdateService, AgentId] | Response:
    """Auth, service and parsed agent id for a per-machine route, or the refusal to send instead.

    Auth is answered first, so an unauthenticated caller never learns whether
    this build has an update surface at all; a missing service (503) outranks an
    unparseable id (400) when both are true.
    """
    if not is_ui_request_authenticated():
        return _error_response("Not authenticated", 401)
    service = _resolve_service()
    parsed_id = _parse_agent_id(agent_id)
    if service is None or parsed_id is None:
        return _error_response("Updates are not available", 503 if service is None else 400)
    return service, parsed_id


def _resolve_target_request(agent_id: str, *, refusal: str) -> tuple[WorkspaceUpdateService, AgentId, str] | Response:
    """Auth, service, agent id, and validated target ref for a per-machine dispatch or schedule route.

    An explicit ``target_ref`` bypasses the availability gate: the user named
    the target, and the run judges whether the ref exists. A request that names
    none falls back to the machine's armed schedule's target, so pressing the
    plain button under "Scheduled to update to X" does not quietly retarget it.
    """
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    # As the bulk routes refuse it: a fresh run would set aside the work the decision is about.
    if service.state_store.get(parsed_id).is_decision_pending:
        return _error_response(_DECISION_PENDING_REFUSAL, 409, code=_DECISION_PENDING_CODE)
    body = request.get_json(silent=True, force=True)
    try:
        write = UiUpdateTargetRequest.model_validate(body if isinstance(body, dict) else {})
    except ValidationError as e:
        logger.debug("Rejected a malformed update target body: {}", e)
        return _error_response("Invalid JSON body", 400)
    target_ref = write.target_ref.strip()
    if target_ref and _OVERRIDE_REF_RE.fullmatch(target_ref) is None:
        return _error_response("That doesn't look like a version, branch, or git ref.", 400, code=_INVALID_REF_CODE)
    if not target_ref and not service.state_store.get(parsed_id).is_update_dispatchable:
        return _error_response(refusal, 409, code=_NOTHING_TO_UPDATE_CODE)
    # After the gate, which keys on what the request itself asked for.
    return service, parsed_id, target_ref or _armed_target_ref(service, parsed_id)


def _handle_update_now(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/now: run the update in this machine right away."""
    resolved = _resolve_target_request(agent_id, refusal="This workspace has no update to run.")
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id, target_ref = resolved
    return _dispatch_response(service.dispatch_update(parsed_id, target_override=target_ref or None))


def _handle_schedule_update(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/schedule: arm this machine's update through its own schedule file.

    A ``target_ref`` is accepted here as on the now route: pressing the version
    field is the confirmation, and scheduling only changes when the run happens.
    """
    resolved = _resolve_target_request(agent_id, refusal="This workspace has no update to schedule.")
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id, target_ref = resolved
    # A schedule armed during a run outlives it: if the run ends waiting on a decision, the
    # window would start a fresh run that sets the kept work aside.
    if service.state_store.get(parsed_id).is_run_in_flight:
        return _error_response(
            DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.ALREADY_RUNNING],
            409,
            code=UpdateDispatchOutcome.ALREADY_RUNNING.value,
        )
    return _schedule_response(service.schedule_update(parsed_id, override_ref=target_ref, is_auto_armed=False))


def _handle_cancel_schedule(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/schedule/cancel: delete this machine's schedule file.

    One that cannot be reached keeps its schedule, and so does its row.
    """
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    refusal = service.cancel_schedule(parsed_id)
    if refusal is not None:
        return _error_response(_CANCEL_FAILURE_MESSAGE, 502, refusal.failure_detail, _CANCEL_UNREACHED_CODE)
    return _json_response({"ok": True})


def _handle_skip_update(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/skip: skip the app's current release for this machine."""
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    refusal = service.skip_update(parsed_id)
    if refusal is not None:
        return _error_response(_SKIP_FAILURE_MESSAGE, 502, refusal.failure_detail, _SKIP_UNREACHED_CODE)
    # The skip lives in the app's settings, which no update-state change announces.
    ui_publisher = get_state().ui_publisher
    if ui_publisher is not None:
        ui_publisher.notify_change()
    return _json_response({"ok": True})


def _handle_sign_in(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/sign-in: open a new chat in the machine with the provider chooser up.

    The press enters the machine first, so the window lands on the screen the user is looking at.
    """
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    if not service.open_sign_in_chat(parsed_id):
        return _error_response(_SIGN_IN_FAILURE_MESSAGE, 502, code=_SIGN_IN_CHAT_FAILED_CODE)
    return _json_response({"ok": True})


class UiKernelFixRequest(FrozenModel):
    """Body of the kernel-fix route: whether to restart even while a chat is working, or the chats cannot be listed."""

    force: bool = Field(default=False, description="Go ahead past a running or unlistable chat")


def _handle_kernel_fix(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/kernel-fix: restart this machine onto the fixed kernel.

    A refusal that a repeated press may get past says so with ``is_force_offered``
    and names the chats in the way, so the surface can ask before it presses again.
    """
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    body = request.get_json(silent=True, force=True)
    try:
        write = UiKernelFixRequest.model_validate(body if isinstance(body, dict) else {})
    except ValidationError as e:
        logger.debug("Rejected a malformed kernel-fix body: {}", e)
        return _error_response("Invalid JSON body", 400)
    dispatch = service.kernel_fix.dispatch(parsed_id, is_forced=write.force)
    if dispatch.outcome is KernelFixDispatchOutcome.DISPATCHED:
        return _json_response({"ok": True})
    payload: dict[str, object] = {
        "error": KERNEL_FIX_REFUSAL_MESSAGE_BY_OUTCOME[dispatch.outcome],
        "code": dispatch.outcome.value,
        "is_force_offered": dispatch.outcome in FORCEABLE_KERNEL_FIX_OUTCOMES,
        "running_chats": list(dispatch.running_chats),
    }
    return _json_response(payload, 409)


def _handle_set_auto_update_override(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/auto-update: set or clear this workspace's own auto-update setting."""
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    body = request.get_json(silent=True, force=True)
    try:
        write = UiAutoUpdateOverrideRequest.model_validate(body if isinstance(body, dict) else {})
        if write.window_start_hour is not None and write.window_start_hour == write.window_end_hour:
            return _error_response(EMPTY_UPDATE_WINDOW_MESSAGE, 400, code=_EMPTY_WINDOW_CODE)
        override = (
            None
            if write.mode is None
            else WorkspaceAutoUpdateOverride(
                mode=write.mode,
                window=(
                    (write.window_start_hour, write.window_end_hour)
                    if write.window_start_hour is not None and write.window_end_hour is not None
                    else None
                ),
            )
        )
    except ValidationError as e:
        logger.debug("Rejected a malformed auto-update setting: {}", e)
        return _error_response("Invalid auto-update setting", 400)
    try:
        is_stored = service.set_auto_update_override(parsed_id, override)
    except (MindsConfigError, OSError) as e:
        logger.warning("Could not save the auto-update setting of {}: {}", parsed_id, e)
        return _error_response(f"Could not save the auto-update setting: {e}", 500)
    if not is_stored:
        return _error_response("Settings storage is not configured", 503)
    # The setting lives in the app's settings, which no update-state change announces.
    ui_publisher = get_state().ui_publisher
    if ui_publisher is not None:
        ui_publisher.notify_change()
    return _json_response({"ok": True})


def _parse_bulk_request() -> UiBulkUpdateRequest | None:
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return None
    try:
        return UiBulkUpdateRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed bulk-update body: {}", e)
        return None


def _eligible_bulk_ids(service: WorkspaceUpdateService, requested: tuple[str, ...]) -> list[AgentId]:
    """The requested workspaces that are confirmed out of date, not already running, and not waiting on a decision.

    Filtered server-side because the SPA's list can be a moment stale. A pending decision is answered in its
    run's chat, which re-lands the kept work; a fresh run would set that work aside.
    """
    eligible: list[AgentId] = []
    for agent_id_str in requested:
        parsed_id = _parse_agent_id(agent_id_str)
        if parsed_id is None:
            continue
        state = service.state_store.get(parsed_id)
        if state.is_update_offered and not state.is_run_in_flight and not state.is_decision_pending:
            eligible.append(parsed_id)
    return eligible


def _resolve_bulk_action() -> tuple[WorkspaceUpdateService, list[AgentId]] | Response:
    """Auth, service, and eligible ids for a bulk route, or the refusal response to send instead."""
    if not is_ui_request_authenticated():
        return _error_response("Not authenticated", 401)
    service = _resolve_service()
    if service is None:
        return _error_response("Updates are not available", 503)
    write = _parse_bulk_request()
    if write is None:
        return _error_response("Invalid JSON body", 400)
    return service, _eligible_bulk_ids(service, write.agent_ids)


def _handle_bulk_now() -> Response:
    """POST /ui/api/updates/bulk/now: start every confirmed out-of-date machine's update at once."""
    resolved = _resolve_bulk_action()
    if isinstance(resolved, Response):
        return resolved
    service, eligible = resolved
    _start_bulk_thread("update-bulk-dispatch", lambda agent_id: _dispatch_one_bulk(service, agent_id), eligible)
    return _json_response({"ok": True, "dispatching": [str(agent_id) for agent_id in eligible]})


def _handle_bulk_schedule() -> Response:
    """POST /ui/api/updates/bulk/schedule: arm every confirmed out-of-date machine's update."""
    resolved = _resolve_bulk_action()
    if isinstance(resolved, Response):
        return resolved
    service, eligible = resolved
    _start_bulk_thread("update-bulk-schedule", lambda agent_id: _schedule_one_bulk(service, agent_id), eligible)
    return _json_response({"ok": True, "scheduled": [str(agent_id) for agent_id in eligible]})


def _start_bulk_thread(name: str, act_on_one: Callable[[AgentId], None], agent_ids: list[AgentId]) -> None:
    """Act on each machine off the request thread: each is an exec that may be a minutes-long cold boot, and
    each outcome reaches the user through its own row anyway."""
    threading.Thread(target=run_bulk_action, args=(act_on_one, agent_ids), name=name, daemon=True).start()


def run_bulk_action(act_on_one: Callable[[AgentId], None], agent_ids: list[AgentId]) -> None:
    """Body of the thread a bulk route spawns: one machine at a time, none of them able to stop the rest."""
    for agent_id in agent_ids:
        try:
            act_on_one(agent_id)
        except (MindError, MngrError, OSError, RuntimeError, ValueError) as e:
            # One failure must not cost the rest of the bulk action; its row reports its own state.
            logger.warning("A bulk update action for {} failed: {}", agent_id, e)


def _dispatch_one_bulk(service: WorkspaceUpdateService, agent_id: AgentId) -> None:
    dispatch = service.dispatch_update(agent_id)
    logger.info("Bulk update dispatch for {}: {}", agent_id, dispatch.log_description)


def _schedule_one_bulk(service: WorkspaceUpdateService, agent_id: AgentId) -> None:
    # Re-arming rewrites the file, so a machine already pointed at a named version keeps it.
    schedule = service.schedule_update_unwatched(
        agent_id, override_ref=_armed_target_ref(service, agent_id), is_auto_armed=False
    )
    logger.info("Bulk update schedule for {}: {}", agent_id, schedule.outcome.value)


def _handle_dismiss_run_outcome(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/dismiss: clear how this machine's last run ended, or why it never went out.

    Separate from the note route so dismissing the good news cannot silently
    clear an unread failure.
    """
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    service.state_store.dismiss_run_outcome(parsed_id)
    return _json_response({"ok": True})


def _handle_dismiss_note(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/note/dismiss: clear this machine's "Updated to X" note."""
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    service.state_store.dismiss_success_note(parsed_id)
    return _json_response({"ok": True})


def register_update_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/updates/<agent_id>/now", view_func=_handle_update_now, methods=["POST"])
    blueprint.add_url_rule("/api/updates/<agent_id>/schedule", view_func=_handle_schedule_update, methods=["POST"])
    blueprint.add_url_rule(
        "/api/updates/<agent_id>/schedule/cancel", view_func=_handle_cancel_schedule, methods=["POST"]
    )
    blueprint.add_url_rule("/api/updates/<agent_id>/skip", view_func=_handle_skip_update, methods=["POST"])
    blueprint.add_url_rule("/api/updates/<agent_id>/sign-in", view_func=_handle_sign_in, methods=["POST"])
    blueprint.add_url_rule("/api/updates/<agent_id>/dismiss", view_func=_handle_dismiss_run_outcome, methods=["POST"])
    blueprint.add_url_rule("/api/updates/<agent_id>/note/dismiss", view_func=_handle_dismiss_note, methods=["POST"])
    blueprint.add_url_rule("/api/updates/<agent_id>/kernel-fix", view_func=_handle_kernel_fix, methods=["POST"])
    blueprint.add_url_rule(
        "/api/updates/<agent_id>/auto-update", view_func=_handle_set_auto_update_override, methods=["POST"]
    )
    blueprint.add_url_rule("/api/updates/bulk/now", view_func=_handle_bulk_now, methods=["POST"])
    blueprint.add_url_rule("/api/updates/bulk/schedule", view_func=_handle_bulk_schedule, methods=["POST"])
