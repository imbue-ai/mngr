"""Settings' Suggestions page for a workspace (specs/chat-suggestions, sections 3.7 and 4.8).

Two routes on ``/ui/api/workspaces/<agent_id>/suggestion-settings`` read and change the
suggestion switches of a workspace. The switches live only in the workspace's chat app, so each
request is passed on to that app's own origin through ``mngr forward`` (``ChatAppForwardRoute``,
the route the welcome chat's seed takes): the page cannot ask the chat app itself, which is
another origin and refuses cross-origin page requests.

A workspace that gave no usable answer, or whose chat app is from before the switches, answers
502 with a ``code`` that says which.
"""

import time
from collections.abc import Mapping
from enum import auto
from typing import Final

import httpx2
from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import StrictBool
from pydantic import ValidationError
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.welcome_chat import ChatAppForwardRoute
from imbue.minds.desktop_client.welcome_chat import chat_app_forward_host
from imbue.minds.desktop_client.welcome_chat import describe_forward_transport_failure
from imbue.minds.desktop_client.welcome_chat import forward_answer_detail
from imbue.minds.errors import EmptySuggestionSettingsChangeError
from imbue.mngr.primitives import AgentId

CHAT_SETTINGS_PATH: Final[str] = "/api/settings"
CHAT_SUGGESTION_SETTINGS_PATH: Final[str] = "/api/settings/suggestions"

# The chat app answers these from a file read or write on the workspace's own disk.
_CHAT_APP_REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
_SLOW_CHAT_APP_REQUEST_WARNING_SECONDS: Final[float] = 3.0

# What a chat app with no such route answers: the workspace's template is from before the switches.
_MISSING_ROUTE_STATUS_CODES: Final[frozenset[int]] = frozenset({404, 405})

_UNREACHABLE_MESSAGE: Final[str] = (
    "Couldn't reach this workspace's suggestion settings. Make sure it's running, then try again."
)
_UNSUPPORTED_MESSAGE: Final[str] = (
    "This workspace doesn't have suggestion settings yet. Update the workspace, then try again."
)
_NO_ROUTE_MESSAGE: Final[str] = "This app cannot reach workspace suggestion settings"


class SuggestionSettings(FrozenModel):
    """A workspace's suggestion switches, as the Suggestions page reads them."""

    is_next_step_suggestions_enabled: bool = Field(description="Whether suggested replies follow an agent's reply")
    is_new_chat_suggestions_enabled: bool = Field(description="Whether suggestions show on a chat with no message")


class SuggestionSettingsChange(FrozenModel):
    """The body of a write: the switches to change, at least one of them."""

    is_next_step_suggestions_enabled: StrictBool | None = Field(
        default=None, description="The next-step switch's new value; None leaves it as it is"
    )
    is_new_chat_suggestions_enabled: StrictBool | None = Field(
        default=None, description="The new-chat switch's new value; None leaves it as it is"
    )

    @model_validator(mode="after")
    def _require_a_switch(self) -> "SuggestionSettingsChange":
        if self.is_next_step_suggestions_enabled is None and self.is_new_chat_suggestions_enabled is None:
            raise EmptySuggestionSettingsChangeError("A suggestion settings change names at least one switch")
        return self


class _ChatAppSuggestionSwitches(FrozenModel):
    """The chat app's settings, read for the switches alone; a chat app from before them sends none."""

    model_config = ConfigDict(extra="ignore")

    is_next_step_suggestions_enabled: StrictBool | None = Field(default=None, description="The next-step switch")
    is_new_chat_suggestions_enabled: StrictBool | None = Field(default=None, description="The new-chat switch")


class _ChatAppSettingsAnswer(FrozenModel):
    """What the chat app's settings routes answer: its settings under one key."""

    model_config = ConfigDict(extra="ignore")

    settings: _ChatAppSuggestionSwitches = Field(
        default_factory=_ChatAppSuggestionSwitches, description="The chat app's settings; none reads as no switches"
    )


class SuggestionSettingsFailureKind(UpperCaseStrEnum):
    """Why a workspace's suggestion switches could not be read or changed; the 502's ``code``."""

    UNREACHABLE = auto()
    """The chat app gave no usable answer: the workspace is down, starting, or failing."""
    UNSUPPORTED = auto()
    """The chat app answered, but it is from before the switches."""


class SuggestionSettingsFailure(FrozenModel):
    """A read or write of the switches that produced no values."""

    kind: SuggestionSettingsFailureKind = Field(description="Which of the two failures this is")
    detail: str = Field(description="What happened, in the words of whatever failed")


_FAILURE_MESSAGE_BY_KIND: Final[dict[SuggestionSettingsFailureKind, str]] = {
    SuggestionSettingsFailureKind.UNREACHABLE: _UNREACHABLE_MESSAGE,
    SuggestionSettingsFailureKind.UNSUPPORTED: _UNSUPPORTED_MESSAGE,
}


@pure
def _unreachable(detail: str) -> SuggestionSettingsFailure:
    return SuggestionSettingsFailure(kind=SuggestionSettingsFailureKind.UNREACHABLE, detail=detail)


@pure
def _unsupported(detail: str) -> SuggestionSettingsFailure:
    return SuggestionSettingsFailure(kind=SuggestionSettingsFailureKind.UNSUPPORTED, detail=detail)


def _read_switches_from_answer(response: httpx2.Response) -> SuggestionSettings | SuggestionSettingsFailure:
    """The switches out of a chat app's answer to either settings route."""
    if response.status_code in _MISSING_ROUTE_STATUS_CODES:
        return _unsupported(f"the chat app has no such route (HTTP {response.status_code})")
    if not 200 <= response.status_code < 300:
        return _unreachable(f"the chat app answered {forward_answer_detail(response)}")
    try:
        answer = _ChatAppSettingsAnswer.model_validate_json(response.content)
    except ValidationError as e:
        logger.debug("Could not read a chat app's settings answer: {}", e)
        return _unreachable("the chat app's answer could not be read")
    switches = answer.settings
    if switches.is_next_step_suggestions_enabled is None or switches.is_new_chat_suggestions_enabled is None:
        return _unsupported("the chat app's settings carry no suggestion switches")
    return SuggestionSettings(
        is_next_step_suggestions_enabled=switches.is_next_step_suggestions_enabled,
        is_new_chat_suggestions_enabled=switches.is_new_chat_suggestions_enabled,
    )


def _ask_chat_app(
    route: ChatAppForwardRoute,
    workspace_agent_id: AgentId,
    method: str,
    path: str,
    # The JSON body of a write; None sends none.
    body: Mapping[str, bool] | None,
) -> SuggestionSettings | SuggestionSettingsFailure:
    """Send one settings request to the workspace's chat app through the forward, and read the switches back."""
    try:
        client = route.make_client()
    except OSError as e:
        # The proxy's CA certificate could not be read.
        return _unreachable(f"the forward's client could not be built: {e}")
    started_at = time.monotonic()
    with client:
        try:
            response = client.request(
                method,
                route.chat_app_url(path),
                headers={"Host": chat_app_forward_host(workspace_agent_id)},
                json=body,
                timeout=_CHAT_APP_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx2.TransportError as e:
            return _unreachable(f"the request failed: {describe_forward_transport_failure(e)}")
    elapsed_seconds = time.monotonic() - started_at
    if elapsed_seconds > _SLOW_CHAT_APP_REQUEST_WARNING_SECONDS:
        logger.warning(
            "Asking the chat app of workspace {} for {} {} took {:.1f}s",
            workspace_agent_id,
            method,
            path,
            elapsed_seconds,
        )
    return _read_switches_from_answer(response)


def read_suggestion_settings(
    route: ChatAppForwardRoute, workspace_agent_id: AgentId
) -> SuggestionSettings | SuggestionSettingsFailure:
    """The workspace's suggestion switches, or why they could not be read."""
    return _ask_chat_app(route, workspace_agent_id, "GET", CHAT_SETTINGS_PATH, None)


def change_suggestion_settings(
    route: ChatAppForwardRoute, workspace_agent_id: AgentId, change: SuggestionSettingsChange
) -> SuggestionSettings | SuggestionSettingsFailure:
    """Change the named switches and leave the rest; all of them as they stand afterwards, or why nothing is known."""
    body = change.model_dump(exclude_none=True)
    return _ask_chat_app(route, workspace_agent_id, "PUT", CHAT_SUGGESTION_SETTINGS_PATH, body)


def _settings_response(
    workspace_agent_id: AgentId, outcome: SuggestionSettings | SuggestionSettingsFailure
) -> Response:
    if isinstance(outcome, SuggestionSettings):
        return make_response(content=outcome.model_dump_json(), media_type="application/json")
    logger.debug(
        "Could not reach the suggestion settings of workspace {} ({}): {}",
        workspace_agent_id,
        outcome.kind,
        outcome.detail,
    )
    return make_json_error_response(
        _FAILURE_MESSAGE_BY_KIND[outcome.kind], 502, detail=outcome.detail, code=str(outcome.kind)
    )


def _route_and_workspace_or_error(agent_id: str) -> tuple[ChatAppForwardRoute, AgentId] | Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    try:
        workspace_agent_id = AgentId(agent_id)
    except InvalidRandomIdError:
        return make_json_error_response("Unknown workspace", 404)
    route = get_state().chat_app_route
    if route is None:
        return make_json_error_response(_NO_ROUTE_MESSAGE, 503)
    return route, workspace_agent_id


def _handle_read_suggestion_settings(agent_id: str) -> Response:
    """GET: the switches, 200; 502 with ``code`` ``UNREACHABLE`` or ``UNSUPPORTED`` when the workspace gave none."""
    resolved = _route_and_workspace_or_error(agent_id)
    if isinstance(resolved, Response):
        return resolved
    route, workspace_agent_id = resolved
    return _settings_response(workspace_agent_id, read_suggestion_settings(route, workspace_agent_id))


def _handle_change_suggestion_settings(agent_id: str) -> Response:
    """PUT: change the switches the body names (a ``SuggestionSettingsChange``); answers as the GET does."""
    resolved = _route_and_workspace_or_error(agent_id)
    if isinstance(resolved, Response):
        return resolved
    route, workspace_agent_id = resolved
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_json_error_response("Invalid JSON body", 400)
    try:
        change = SuggestionSettingsChange.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed suggestion settings change: {}", e)
        return make_json_error_response("Invalid JSON body", 400)
    return _settings_response(workspace_agent_id, change_suggestion_settings(route, workspace_agent_id, change))


def register_suggestion_settings_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/suggestion-settings",
        view_func=_handle_read_suggestion_settings,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/suggestion-settings",
        view_func=_handle_change_suggestion_settings,
        methods=["PUT"],
    )
