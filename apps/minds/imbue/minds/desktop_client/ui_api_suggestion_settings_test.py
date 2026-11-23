import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx2
import pytest
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from imbue.minds.desktop_client.conftest import SHARED_TEST_HOST_ID
from imbue.minds.desktop_client.conftest import SHARED_TEST_WORKSPACE_ID
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_shared_share
from imbue.minds.desktop_client.conftest import make_shared_with_me_store_for_test
from imbue.minds.desktop_client.conftest import seed_shared_with_me_listing
from imbue.minds.desktop_client.shared_with_me_gate import NOT_OWNED_ERROR_CODE
from imbue.minds.desktop_client.testing import FAKE_FORWARD_PORT
from imbue.minds.desktop_client.testing import ForwardHandler
from imbue.minds.desktop_client.testing import answering_forward
from imbue.minds.desktop_client.testing import make_fake_chat_app_forward_route
from imbue.minds.desktop_client.welcome_chat import ChatAppForwardRoute
from imbue.mngr.utils.testing import capture_loguru

_AGENT_ID = "agent-" + "5" * 32
_ROUTE_URL = f"/ui/api/workspaces/{_AGENT_ID}/suggestion-settings"
_CHAT_ORIGIN_HOST = f"chat.{_AGENT_ID}.localhost"
_CHAT_SETTINGS_URL = f"https://127.0.0.1:{FAKE_FORWARD_PORT}/api/settings"
_CHAT_SUGGESTION_SETTINGS_URL = f"https://127.0.0.1:{FAKE_FORWARD_PORT}/api/settings/suggestions"
_RECIPIENT_USER_ID = "55555555-5555-5555-5555-555555555555"
_RECIPIENT_EMAIL = "viewer@example.com"

# A chat app's settings as it answers them: its other settings ride along with the two switches.
_CHAT_SETTINGS = httpx2.Response(
    200,
    json={
        "settings": {
            "is_enter_to_send": True,
            "is_next_step_suggestions_enabled": True,
            "is_new_chat_suggestions_enabled": False,
            "is_picker_send_hint_shown": False,
        }
    },
)
_SWITCHES = {
    "is_next_step_suggestions_enabled": True,
    "is_new_chat_suggestions_enabled": False,
}
_UNREACHABLE_ERROR = "Couldn't reach this workspace's suggestion settings. Make sure it's running, then try again."
_UNSUPPORTED_ERROR = "This workspace doesn't have suggestion settings yet. Update the workspace, then try again."


def _client(tmp_path: Path, handler: ForwardHandler, is_authenticated: bool = True) -> FlaskClient:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=is_authenticated, chat_app_route=make_fake_chat_app_forward_route(handler)
    )
    return client


def _json(response: TestResponse) -> dict[str, object]:
    return json.loads(response.get_data(as_text=True))


def test_the_read_asks_the_workspaces_chat_app_through_the_forward_and_answers_the_two_switches_alone(
    tmp_path: Path,
) -> None:
    requests, handler = answering_forward(_CHAT_SETTINGS)

    response = _client(tmp_path, handler).get(_ROUTE_URL)

    assert response.status_code == 200
    assert _json(response) == _SWITCHES
    (request,) = requests
    assert request.method == "GET"
    assert str(request.url) == _CHAT_SETTINGS_URL
    assert request.headers["host"] == _CHAT_ORIGIN_HOST
    assert request.content == b""


def test_the_write_forwards_only_the_switches_it_was_given_and_answers_them_as_they_stand_afterwards(
    tmp_path: Path,
) -> None:
    requests, handler = answering_forward(_CHAT_SETTINGS)

    response = _client(tmp_path, handler).put(_ROUTE_URL, json={"is_new_chat_suggestions_enabled": False})

    assert response.status_code == 200
    assert _json(response) == _SWITCHES
    (request,) = requests
    assert request.method == "PUT"
    assert str(request.url) == _CHAT_SUGGESTION_SETTINGS_URL
    assert request.headers["host"] == _CHAT_ORIGIN_HOST
    assert json.loads(request.content) == {"is_new_chat_suggestions_enabled": False}


def test_the_write_forwards_every_switch_named_together(tmp_path: Path) -> None:
    requests, handler = answering_forward(_CHAT_SETTINGS)
    body = {
        "is_next_step_suggestions_enabled": False,
        "is_new_chat_suggestions_enabled": False,
    }

    response = _client(tmp_path, handler).put(_ROUTE_URL, json=body)

    assert response.status_code == 200
    assert json.loads(requests[0].content) == body


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"is_new_chat_suggestions_enabled": None},
        {"is_new_chat_suggestions_enabled": "false"},
        {"is_new_chat_suggestions_enabled": 0},
        {"is_new_chat_suggestions_enabled": False, "is_enter_to_send": False},
    ],
)
def test_a_write_naming_no_switch_a_non_boolean_or_another_setting_is_refused_before_the_workspace_is_asked(
    tmp_path: Path, body: dict[str, object]
) -> None:
    requests, handler = answering_forward(_CHAT_SETTINGS)

    response = _client(tmp_path, handler).put(_ROUTE_URL, json=body)

    assert response.status_code == 400
    assert _json(response) == {"error": "Invalid JSON body"}
    assert requests == []


def test_a_write_whose_body_is_not_a_json_object_is_refused(tmp_path: Path) -> None:
    requests, handler = answering_forward(_CHAT_SETTINGS)
    client = _client(tmp_path, handler)

    assert client.put(_ROUTE_URL, data="not json").status_code == 400
    assert client.put(_ROUTE_URL, json=[True]).status_code == 400
    assert requests == []


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx2.ConnectError("connection refused"), "the request failed: ConnectError: connection refused"),
        (httpx2.ReadTimeout("timed out"), "the request failed: ReadTimeout: timed out"),
        (httpx2.Response(503, text="Backend not yet available"), "the chat app answered HTTP 503"),
        (httpx2.Response(403, text="Not authenticated"), "the chat app answered HTTP 403"),
        (
            httpx2.Response(500, json={"detail": "The settings file could not be read"}),
            "the chat app answered The settings file could not be read",
        ),
        (httpx2.Response(200, text="<html>not the chat app</html>"), "the chat app's answer could not be read"),
        (
            httpx2.Response(200, json={"settings": {**_SWITCHES, "is_next_step_suggestions_enabled": "on"}}),
            "the chat app's answer could not be read",
        ),
    ],
)
def test_a_workspace_that_gives_no_usable_answer_is_a_502_coded_unreachable_for_the_read_and_the_write(
    tmp_path: Path, answer: httpx2.Response | Exception, detail: str
) -> None:
    requests, handler = answering_forward(answer)
    client = _client(tmp_path, handler)

    read = client.get(_ROUTE_URL)
    write = client.put(_ROUTE_URL, json={"is_new_chat_suggestions_enabled": False})

    for response in (read, write):
        assert response.status_code == 502
        assert _json(response) == {"error": _UNREACHABLE_ERROR, "code": "UNREACHABLE", "detail": detail}
    # Neither is retried: the page shows the group unavailable, or puts the switch back.
    assert [request.method for request in requests] == ["GET", "PUT"]


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx2.Response(404, json={"detail": "Not Found"}), "the chat app has no such route (HTTP 404)"),
        (httpx2.Response(405, json={"detail": "Method Not Allowed"}), "the chat app has no such route (HTTP 405)"),
        (
            httpx2.Response(200, json={"settings": {"is_enter_to_send": True}}),
            "the chat app's settings carry no suggestion switches",
        ),
        (
            httpx2.Response(
                200,
                json={"settings": {"is_next_step_suggestions_enabled": True}},
            ),
            "the chat app's settings carry no suggestion switches",
        ),
        (
            httpx2.Response(200, json={"is_enter_to_send": True}),
            "the chat app's settings carry no suggestion switches",
        ),
    ],
)
def test_a_chat_app_from_before_the_switches_is_a_502_coded_unsupported_for_the_read_and_the_write(
    tmp_path: Path, answer: httpx2.Response, detail: str
) -> None:
    _requests, handler = answering_forward(answer)
    client = _client(tmp_path, handler)

    read = client.get(_ROUTE_URL)
    write = client.put(_ROUTE_URL, json={"is_new_chat_suggestions_enabled": False})

    for response in (read, write):
        assert response.status_code == 502
        assert _json(response) == {"error": _UNSUPPORTED_ERROR, "code": "UNSUPPORTED", "detail": detail}


def test_a_route_whose_client_cannot_be_built_is_unreachable(tmp_path: Path) -> None:
    def no_ca_certificate() -> httpx2.Client:
        raise FileNotFoundError("no such file: forward-ca.pem")

    route = ChatAppForwardRoute(forward_port=FAKE_FORWARD_PORT, make_client=no_ca_certificate)
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True, chat_app_route=route)

    response = client.get(_ROUTE_URL)

    assert response.status_code == 502
    assert _json(response) == {
        "error": _UNREACHABLE_ERROR,
        "code": "UNREACHABLE",
        "detail": "the forward's client could not be built: no such file: forward-ca.pem",
    }


def test_a_failed_read_is_logged_with_what_failed_and_a_good_one_is_quiet(tmp_path: Path) -> None:
    _requests, failing = answering_forward(httpx2.Response(503, text="Backend not yet available"))
    _requests, answering = answering_forward(_CHAT_SETTINGS)

    with capture_loguru(level="DEBUG") as failed_log:
        _client(tmp_path / "failing", failing).get(_ROUTE_URL)
    with capture_loguru(level="DEBUG") as good_log:
        _client(tmp_path / "answering", answering).get(_ROUTE_URL)

    failure_line = (
        f"Could not reach the suggestion settings of workspace {_AGENT_ID} (UNREACHABLE): "
        "the chat app answered HTTP 503"
    )
    assert failure_line in failed_log.getvalue().splitlines()
    assert "suggestion settings" not in good_log.getvalue()


def test_an_app_with_no_forward_to_reach_the_chat_app_through_answers_503(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    for response in (client.get(_ROUTE_URL), client.put(_ROUTE_URL, json={"is_new_chat_suggestions_enabled": True})):
        assert response.status_code == 503
        assert _json(response) == {"error": "This app cannot reach workspace suggestion settings"}


def test_the_routes_require_the_ui_session_and_a_workspace_id(tmp_path: Path) -> None:
    requests, handler = answering_forward(_CHAT_SETTINGS)
    signed_out = _client(tmp_path / "signed-out", handler, is_authenticated=False)
    signed_in = _client(tmp_path / "signed-in", handler)

    assert signed_out.get(_ROUTE_URL).status_code == 401
    assert signed_out.put(_ROUTE_URL, json={"is_new_chat_suggestions_enabled": True}).status_code == 401
    assert signed_in.get("/ui/api/workspaces/not-an-id/suggestion-settings").status_code == 404
    not_an_id = signed_in.put(
        "/ui/api/workspaces/not-an-id/suggestion-settings", json={"is_new_chat_suggestions_enabled": True}
    )
    assert not_an_id.status_code == 404
    assert requests == []


def test_the_routes_refuse_a_workspace_shared_with_an_account_here_without_asking_it(tmp_path: Path) -> None:
    """The switches are the owner's: the shared-with-me gate reads the ``agent_id`` the routes name."""
    cli = make_fake_imbue_cloud_cli()
    seed_shared_with_me_listing(
        cli, _RECIPIENT_EMAIL, make_shared_share(SHARED_TEST_HOST_ID, SHARED_TEST_WORKSPACE_ID)
    )
    store = make_shared_with_me_store_for_test(tmp_path / "minds", cli)
    assert store.refresh(_RECIPIENT_USER_ID, _RECIPIENT_EMAIL, datetime.now(timezone.utc)) is True
    requests, handler = answering_forward(_CHAT_SETTINGS)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        chat_app_route=make_fake_chat_app_forward_route(handler),
        shared_with_me_store=store,
    )
    shared_url = f"/ui/api/workspaces/{SHARED_TEST_WORKSPACE_ID}/suggestion-settings"

    read = client.get(shared_url)
    write = client.put(shared_url, json={"is_new_chat_suggestions_enabled": True})

    for response in (read, write):
        assert response.status_code == 403
        assert _json(response)["code"] == NOT_OWNED_ERROR_CODE
    assert requests == []
