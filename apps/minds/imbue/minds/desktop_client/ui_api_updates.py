"""The SPA's update surface: the channel payload plus the /ui/api/updates routes.

Single-workspace dispatch needs only ``is_update_dispatchable`` (an unreadable
version is fine: ``/update-self`` reads the workspace's own upstream). Bulk
actions claim "update everything that is behind", so they cover only
``is_update_offered``.
"""

import json
import re
import threading
from typing import Final

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdatesMessage
from imbue.minds.desktop_client.update_scheduler import UpdateScheduler
from imbue.minds.desktop_client.update_service import UpdateDispatchOutcome
from imbue.minds.desktop_client.update_service import WorkspaceUpdateService
from imbue.minds.errors import MindError
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId

# 409 for a workspace that cannot host the skill, 502 for one we could not
# reach -- the same split the /assist dispatch makes.
_DISPATCH_STATUS_BY_OUTCOME: Final[dict[UpdateDispatchOutcome, int]] = {
    UpdateDispatchOutcome.DISPATCHED: 200,
    UpdateDispatchOutcome.ALREADY_RUNNING: 409,
    UpdateDispatchOutcome.UNSUPPORTED: 409,
    UpdateDispatchOutcome.UNREACHABLE: 502,
    UpdateDispatchOutcome.SPAWN_FAILED: 502,
}

_DISPATCH_MESSAGE_BY_OUTCOME: Final[dict[UpdateDispatchOutcome, str]] = {
    UpdateDispatchOutcome.ALREADY_RUNNING: "An update is already running in this machine.",
    UpdateDispatchOutcome.UNSUPPORTED: (
        "This machine is too old to update itself. Ask an agent inside it for help, "
        "or create a new machine and migrate your work."
    ),
    UpdateDispatchOutcome.UNREACHABLE: "Couldn't reach this machine to start the update.",
    UpdateDispatchOutcome.SPAWN_FAILED: "Couldn't start the update agent in this machine. Please try again.",
}


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


def format_update_window(window: tuple[int, int]) -> str:
    """Render the configured hour window the way a person reads a clock."""
    return f"{_format_hour(window[0])}-{_format_hour(window[1])}"


def _format_hour(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:00 {suffix}"


def build_workspace_updates_message(
    service: WorkspaceUpdateService, update_window: tuple[int, int]
) -> UiWorkspaceUpdatesMessage:
    """The store's composed state, plus the one per-frame fact it does not hold."""
    return UiWorkspaceUpdatesMessage(
        updates={
            agent_id_str: state.model_copy_update(
                # The SPA's no-backups confirmation keys on this, so the server never refuses.
                to_update(state.field_ref().is_backup_configured, service.is_backup_configured(AgentId(agent_id_str)))
            )
            for agent_id_str, state in service.state_store.snapshot().items()
        },
        update_window=format_update_window(update_window),
    )


def _json_response(payload: dict[str, object], status_code: int = 200) -> Response:
    return make_response(status_code=status_code, content=json.dumps(payload), media_type="application/json")


def _error_response(message: str, status_code: int) -> Response:
    return _json_response({"error": message}, status_code)


def _resolve_service() -> WorkspaceUpdateService | None:
    return get_state().workspace_update_service


def _parse_agent_id(agent_id: str) -> AgentId | None:
    try:
        return AgentId(agent_id)
    except ValueError:
        return None


def _dispatch_response(outcome: UpdateDispatchOutcome) -> Response:
    status = _DISPATCH_STATUS_BY_OUTCOME[outcome]
    if status == 200:
        return _json_response({"ok": True})
    return _error_response(_DISPATCH_MESSAGE_BY_OUTCOME[outcome], status)


def _armed_target_ref(service: WorkspaceUpdateService, agent_id: AgentId) -> str:
    """The version this machine's armed schedule names, or '' when nothing is armed.

    The only way to clear one is to cancel the schedule: both version-field buttons refuse an empty
    field, so an empty request never means "put this machine back on the default target".
    """
    record = service.schedule_store.read(agent_id)
    return record.target_ref if record is not None else ""


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
    body = request.get_json(silent=True, force=True)
    try:
        write = UiUpdateTargetRequest.model_validate(body if isinstance(body, dict) else {})
    except ValidationError as e:
        logger.debug("Rejected a malformed update target body: {}", e)
        return _error_response("Invalid JSON body", 400)
    target_ref = write.target_ref.strip()
    if target_ref and _OVERRIDE_REF_RE.fullmatch(target_ref) is None:
        return _error_response("That doesn't look like a version, branch, or git ref.", 400)
    if not target_ref and not service.state_store.get(parsed_id).is_update_dispatchable:
        return _error_response(refusal, 409)
    # After the gate, which keys on what the request itself asked for.
    return service, parsed_id, target_ref or _armed_target_ref(service, parsed_id)


def _handle_update_now(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/now: run the update in this machine right away."""
    resolved = _resolve_target_request(agent_id, refusal="This machine has no update to run.")
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id, target_ref = resolved
    return _dispatch_response(service.dispatch_update(parsed_id, target_override=target_ref or None))


def _handle_schedule_update(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/schedule: arm a scheduled update for this machine.

    A ``target_ref`` is accepted here as on the now route: pressing the version
    field is the confirmation, and scheduling only changes when the run happens.
    """
    resolved = _resolve_target_request(agent_id, refusal="This machine has no update to schedule.")
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id, target_ref = resolved
    service.schedule_store.schedule(parsed_id, target_ref=target_ref)
    return _json_response({"ok": True})


def _handle_cancel_schedule(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/schedule/cancel: disarm this machine's scheduled update."""
    resolved = _resolve_machine_request(agent_id)
    if isinstance(resolved, Response):
        return resolved
    service, parsed_id = resolved
    service.schedule_store.cancel(parsed_id)
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
    """The requested workspaces that are confirmed out of date and not already running.

    Filtered server-side because the SPA's list can be a moment stale.
    """
    eligible: list[AgentId] = []
    for agent_id_str in requested:
        parsed_id = _parse_agent_id(agent_id_str)
        if parsed_id is None:
            continue
        state = service.state_store.get(parsed_id)
        if state.is_update_offered and not state.is_run_in_flight:
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
    """POST /ui/api/updates/bulk/now: dispatch every confirmed out-of-date machine at once.

    Through the schedule's skip gate: "now" changes when the run happens, not
    what is safe to do to a machine agents are working in.
    """
    resolved = _resolve_bulk_action()
    if isinstance(resolved, Response):
        return resolved
    service, eligible = resolved
    # Off the request thread: each dispatch may be a minutes-long cold boot, and
    # each outcome reaches the user through its own row anyway.
    threading.Thread(
        target=run_bulk_dispatch,
        args=(service, get_state().update_scheduler, eligible),
        name="update-bulk-dispatch",
        daemon=True,
    ).start()
    return _json_response({"ok": True, "dispatching": [str(agent_id) for agent_id in eligible]})


def run_bulk_dispatch(
    service: WorkspaceUpdateService, scheduler: UpdateScheduler | None, agent_ids: list[AgentId]
) -> None:
    """Body of the thread spawned by the bulk-now route; each machine goes through the schedule's gate."""
    for agent_id in agent_ids:
        try:
            _dispatch_one_bulk(service, scheduler, agent_id)
        except (MindError, MngrError, OSError, RuntimeError, ValueError) as e:
            # One failure must not cost the rest of the bulk action; its row reports its own state.
            logger.warning("Bulk update dispatch for {} failed: {}", agent_id, e)


def _dispatch_one_bulk(service: WorkspaceUpdateService, scheduler: UpdateScheduler | None, agent_id: AgentId) -> None:
    """Run one machine's bulk-now attempt, logging what became of it."""
    if scheduler is None:
        # No scheduler means no gate to apply; dispatch directly rather than do nothing.
        outcome = service.dispatch_update(agent_id)
        logger.info("Bulk update dispatch for {}: {}", agent_id, outcome.value)
        return
    skip_reason = scheduler.run_now(agent_id)
    if skip_reason is None:
        logger.info("Bulk update dispatch for {} went out", agent_id)
    else:
        logger.info("Skipped the bulk update for {}: {}", agent_id, skip_reason.value)


def _handle_bulk_schedule() -> Response:
    """POST /ui/api/updates/bulk/schedule: arm a scheduled update for every confirmed out-of-date machine."""
    resolved = _resolve_bulk_action()
    if isinstance(resolved, Response):
        return resolved
    service, eligible = resolved
    for agent_id in eligible:
        # Re-arming replaces the record, so a machine already pointed at a named
        # version has to be re-armed at that same version.
        service.schedule_store.schedule(agent_id, target_ref=_armed_target_ref(service, agent_id))
    return _json_response({"ok": True, "scheduled": [str(agent_id) for agent_id in eligible]})


def _handle_dismiss_run_outcome(agent_id: str) -> Response:
    """POST /ui/api/updates/<agent_id>/dismiss: clear how this machine's last run ended.

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
    blueprint.add_url_rule("/api/updates/<agent_id>/dismiss", view_func=_handle_dismiss_run_outcome, methods=["POST"])
    blueprint.add_url_rule("/api/updates/<agent_id>/note/dismiss", view_func=_handle_dismiss_note, methods=["POST"])
    blueprint.add_url_rule("/api/updates/bulk/now", view_func=_handle_bulk_now, methods=["POST"])
    blueprint.add_url_rule("/api/updates/bulk/schedule", view_func=_handle_bulk_schedule, methods=["POST"])
