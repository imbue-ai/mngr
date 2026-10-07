"""`/ui/api/provider-relay`: arm the relay for a workspace's provider sign-in and open it, or stop it.

Chrome-only: the chrome calls it when the workspace frame it shows asks for a sign-in, naming
that frame's workspace itself. Both routes require a JSON body, so a cross-origin page cannot
reach them without a preflight the desktop client never answers.
"""

import json
from typing import Final
from typing import TypeVar

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.provider_relay import AFTER_CALLBACK_SECONDS
from imbue.minds.desktop_client.provider_relay import CHAT_SERVICE_NAME
from imbue.minds.desktop_client.provider_relay import InvalidSignInUrlError
from imbue.minds.desktop_client.provider_relay import NO_CALLBACK_TIMEOUT_SECONDS
from imbue.minds.desktop_client.provider_relay import ProviderSignInRelay
from imbue.minds.desktop_client.provider_relay import RelayArmResult
from imbue.minds.desktop_client.provider_relay import WorkspaceChatSignInForwarder
from imbue.minds.desktop_client.provider_relay import parse_provider_sign_in_url
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_models import UiBringAppToFrontMessage
from imbue.minds.desktop_client.ui_publisher import UiStatePublisher
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId

_FLOW_ID_PATTERN: Final[str] = r"^[A-Za-z0-9_-]{1,128}$"
_MAX_SIGN_IN_URL_LENGTH: Final[int] = 8192
_RequestT = TypeVar("_RequestT", bound=FrozenModel)


class UiProviderRelayRequest(FrozenModel):
    """Which workspace flow a sign-in belongs to, and the provider URL to open for it."""

    workspace_id: AgentId = Field(description="The workspace whose chat app runs the sign-in")
    flow_id: str = Field(pattern=_FLOW_ID_PATTERN, description="The chat app's sign-in flow id")
    url: str = Field(max_length=_MAX_SIGN_IN_URL_LENGTH, description="The provider's authorize URL")


class UiProviderRelayStopRequest(FrozenModel):
    """The sign-in whose relay is no longer needed."""

    workspace_id: AgentId = Field(description="The workspace whose chat app ran the sign-in")
    flow_id: str = Field(pattern=_FLOW_ID_PATTERN, description="The chat app's sign-in flow id")


class UiProviderRelayResult(FrozenModel):
    """Whether the relay is listening and the sign-in page was opened."""

    relay: bool = Field(description="False when this machine cannot relay; the workspace falls back to manual sign-in")


def _error_response(message: str, status_code: int) -> Response:
    return make_response(
        content=json.dumps({"error": message}), status_code=status_code, media_type="application/json"
    )


def _result_response(is_relaying: bool) -> Response:
    return make_response(
        content=UiProviderRelayResult(relay=is_relaying).model_dump_json(),
        status_code=200,
        media_type="application/json",
    )


class _AppWindowRaiser(FrozenModel):
    """Raises the window showing the workspace once its relayed sign-in lands.

    Bound to the publisher while the arming request is in context: the relay calls it from its own
    listener thread, where there is no Flask app to look the state up on.
    """

    publisher: UiStatePublisher | None = Field(description="Where the bring-to-front frame is published")
    workspace_id: AgentId = Field(description="The workspace whose sign-in it is")

    def raise_window(self) -> None:
        if self.publisher is not None:
            self.publisher.publish_one_shot(UiBringAppToFrontMessage(agent_id=str(self.workspace_id)))


def _parse_json_body(model: type[_RequestT]) -> _RequestT | Response:
    if not is_ui_request_authenticated():
        return _error_response("Not authenticated", 401)
    # Not force=True: a JSON content type is what makes a cross-origin caller need a preflight.
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error_response("Expected a JSON body", 400)
    try:
        return model.model_validate(body)
    except ValidationError as e:
        logger.debug("Refused a provider sign-in relay request: {}", type(e).__name__)
        return _error_response("Not a provider sign-in this app can relay", 400)


def _handle_provider_relay() -> Response:
    relay_request = _parse_json_body(UiProviderRelayRequest)
    if isinstance(relay_request, Response):
        return relay_request
    try:
        callback = parse_provider_sign_in_url(relay_request.url)
    except InvalidSignInUrlError as e:
        logger.debug("Refused a provider sign-in relay request: {}", e)
        return _error_response("Not a provider sign-in this app can relay", 400)

    state = get_state()
    if state.mngr_forward_port == 0 or not state.mngr_forward_preauth_cookie:
        logger.warning("Cannot relay a provider sign-in: the local forward channel is not configured")
        return _result_response(is_relaying=False)
    chat_service_label = state.backend_resolver.list_service_labels_for_agent(relay_request.workspace_id).get(
        ServiceName(CHAT_SERVICE_NAME), CHAT_SERVICE_NAME
    )
    relay = ProviderSignInRelay(
        workspace_id=relay_request.workspace_id,
        flow_id=relay_request.flow_id,
        callback=callback,
        forwarder=WorkspaceChatSignInForwarder(
            mngr_forward_port=state.mngr_forward_port,
            preauth_cookie=state.mngr_forward_preauth_cookie,
            workspace_id=relay_request.workspace_id,
            chat_service_label=chat_service_label,
            flow_id=relay_request.flow_id,
        ),
        on_callback_handled=_AppWindowRaiser(
            publisher=state.ui_publisher, workspace_id=relay_request.workspace_id
        ).raise_window,
        no_callback_timeout_seconds=NO_CALLBACK_TIMEOUT_SECONDS,
        after_callback_seconds=AFTER_CALLBACK_SECONDS,
    )
    registry = state.provider_relay_registry
    arm_result = registry.arm(relay)
    if arm_result is RelayArmResult.PORT_UNAVAILABLE:
        return _result_response(is_relaying=False)
    if not registry.claim_browser_open(relay_request.workspace_id):
        return _result_response(is_relaying=True)
    browser_id = state.minds_config.get_sign_in_browser_id() if state.minds_config is not None else None
    if not state.sign_in_browsers.open_sign_in_url(relay_request.url, browser_id):
        logger.warning("No browser took the provider sign-in page; giving the relay up")
        registry.disarm(relay_request.workspace_id, relay_request.flow_id)
        return _result_response(is_relaying=False)
    logger.debug(
        "Opened a provider sign-in relayed on port {} for workspace {}", callback.port, relay_request.workspace_id
    )
    return _result_response(is_relaying=True)


def _handle_provider_relay_stop() -> Response:
    stop_request = _parse_json_body(UiProviderRelayStopRequest)
    if isinstance(stop_request, Response):
        return stop_request
    get_state().provider_relay_registry.disarm(stop_request.workspace_id, stop_request.flow_id)
    return make_response(content="{}", status_code=200, media_type="application/json")


def register_provider_relay_routes(blueprint: Blueprint) -> None:
    blueprint.add_url_rule("/api/provider-relay", view_func=_handle_provider_relay, methods=["POST"])
    blueprint.add_url_rule("/api/provider-relay/stop", view_func=_handle_provider_relay_stop, methods=["POST"])
