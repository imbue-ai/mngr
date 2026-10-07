import json
import socket
from pathlib import Path
from urllib.parse import urlencode

import httpx
from flask import Flask
from flask.testing import FlaskClient

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.mock_sign_in_browser_test import RecordingSignInBrowsers
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.testing import scripted_chat_callback_server
from imbue.mngr.utils.testing import find_free_port

_WORKSPACE_ID = "agent-" + "4c" * 16
_FLOW_ID = "5e0a9f6c1b2d4e8fa7c3b19d0e6f2a41"
_STATE = "state-c7e21a93"


def _sign_in_url(callback_port: int) -> str:
    query = {"redirect_uri": f"http://localhost:{callback_port}/callback", "state": _STATE, "client_id": "client-9d"}
    return f"https://claude.ai/oauth/authorize?{urlencode(query)}"


def _build_client(
    tmp_path: Path, opener: RecordingSignInBrowsers, is_authenticated: bool = True, mngr_forward_port: int = 8421
) -> tuple[FlaskClient, Flask]:
    client, app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie="preauth-cookie-3f1e",
        minds_config=MindsConfig(data_dir=tmp_path / "minds"),
        sign_in_browsers=opener,
    )
    return client, app


def _arm(client: FlaskClient, url: str) -> tuple[int, dict[str, object]]:
    response = client.post(
        "/ui/api/provider-relay", json={"workspace_id": _WORKSPACE_ID, "flow_id": _FLOW_ID, "url": url}
    )
    return response.status_code, json.loads(response.get_data(as_text=True))


def test_provider_relay_listens_on_the_callback_port_and_opens_the_page_in_the_chosen_browser(tmp_path: Path) -> None:
    opener = RecordingSignInBrowsers()
    client, app = _build_client(tmp_path, opener)
    minds_config = get_state(app).minds_config
    assert minds_config is not None
    minds_config.set_sign_in_browser_id("/Applications/Firefox.app")
    callback_port = find_free_port()
    url = _sign_in_url(callback_port)

    try:
        status, body = _arm(client, url)
        refused = httpx.get(f"http://127.0.0.1:{callback_port}/callback?state=state-forged-11", timeout=10.0)
    finally:
        get_state(app).provider_relay_registry.stop_all()

    assert (status, body) == (200, {"relay": True})
    assert opener.opened() == [(url, "/Applications/Firefox.app")]
    # The relay, not the page, answers on the callback port -- and refuses a forged state.
    assert refused.status_code == 400


def test_a_relayed_callback_ends_on_the_signed_in_page_and_raises_the_app(tmp_path: Path) -> None:
    answer = {"state": "ok", "detail": None, "account_id": "acct-5d", "provider_name": "Anthropic"}
    with scripted_chat_callback_server(200, json.dumps(answer).encode()) as (forward_port, recorded):
        client, app = _build_client(tmp_path, RecordingSignInBrowsers(), mngr_forward_port=forward_port)
        broadcaster = get_state(app).ui_channel_broadcaster
        frames = broadcaster.register()
        callback_port = find_free_port()
        try:
            _arm(client, _sign_in_url(callback_port))
            page = httpx.get(f"http://127.0.0.1:{callback_port}/callback?code=c-81&state={_STATE}", timeout=30.0)
        finally:
            get_state(app).provider_relay_registry.stop_all()
            broadcaster.unregister(frames)

    assert page.status_code == 200
    assert "You&#x27;re signed in to Anthropic" in page.text
    assert len(recorded) == 1
    # Named for its workspace, so only the window showing it comes forward.
    expected_frame = json.dumps({"type": "bring_app_to_front", "agent_id": _WORKSPACE_ID}, separators=(",", ":"))
    assert expected_frame in [frames.get_nowait() for _ in range(frames.qsize())]


def test_provider_relay_stop_gives_the_port_up(tmp_path: Path) -> None:
    client, app = _build_client(tmp_path, RecordingSignInBrowsers())
    callback_port = find_free_port()
    try:
        _arm(client, _sign_in_url(callback_port))
        stopped = client.post("/ui/api/provider-relay/stop", json={"workspace_id": _WORKSPACE_ID, "flow_id": _FLOW_ID})
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", callback_port))
    finally:
        get_state(app).provider_relay_registry.stop_all()

    assert stopped.status_code == 200


def test_provider_relay_leaves_a_port_another_workspace_is_signing_in_on(tmp_path: Path) -> None:
    opener = RecordingSignInBrowsers()
    client, app = _build_client(tmp_path, opener)
    url = _sign_in_url(find_free_port())
    try:
        first_status, first_body = _arm(client, url)
        second = client.post(
            "/ui/api/provider-relay",
            json={"workspace_id": "agent-" + "7b" * 16, "flow_id": "flow-other-83", "url": url},
        )
    finally:
        get_state(app).provider_relay_registry.stop_all()

    assert (first_status, first_body) == (200, {"relay": True})
    assert (second.status_code, json.loads(second.get_data(as_text=True))) == (200, {"relay": False})
    assert opener.opened() == [(url, None)]


def test_provider_relay_reports_it_cannot_relay_when_the_callback_port_is_taken(tmp_path: Path) -> None:
    opener = RecordingSignInBrowsers()
    client, _app = _build_client(tmp_path, opener)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant:
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()

        status, body = _arm(client, _sign_in_url(occupant.getsockname()[1]))

    assert (status, body) == (200, {"relay": False})
    assert opener.opened() == []


def test_provider_relay_gives_the_port_up_when_no_browser_takes_the_page(tmp_path: Path) -> None:
    client, _app = _build_client(tmp_path, RecordingSignInBrowsers(is_opening=False))
    callback_port = find_free_port()

    status, body = _arm(client, _sign_in_url(callback_port))

    assert (status, body) == (200, {"relay": False})
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", callback_port))


def test_provider_relay_reports_it_cannot_relay_without_the_forward_channel(tmp_path: Path) -> None:
    opener = RecordingSignInBrowsers()
    client, _app = _build_client(tmp_path, opener, mngr_forward_port=0)

    status, body = _arm(client, _sign_in_url(find_free_port()))

    assert (status, body) == (200, {"relay": False})
    assert opener.opened() == []


def test_provider_relay_refuses_a_url_that_is_not_a_provider_sign_in(tmp_path: Path) -> None:
    opener = RecordingSignInBrowsers()
    client, _app = _build_client(tmp_path, opener)
    url = _sign_in_url(find_free_port()).replace("https://claude.ai/", "https://evil.example\\@claude.ai/")

    status, _body = _arm(client, url)

    assert status == 400
    assert opener.opened() == []


def test_provider_relay_refuses_a_body_that_is_not_json(tmp_path: Path) -> None:
    opener = RecordingSignInBrowsers()
    client, _app = _build_client(tmp_path, opener)
    body = json.dumps({"workspace_id": _WORKSPACE_ID, "flow_id": _FLOW_ID, "url": _sign_in_url(find_free_port())})

    response = client.post("/ui/api/provider-relay", data=body, content_type="text/plain")

    assert response.status_code == 400
    assert opener.opened() == []


def test_provider_relay_refuses_a_malformed_flow_id(tmp_path: Path) -> None:
    opener = RecordingSignInBrowsers()
    client, _app = _build_client(tmp_path, opener)

    response = client.post(
        "/ui/api/provider-relay",
        json={"workspace_id": _WORKSPACE_ID, "flow_id": "../../etc", "url": _sign_in_url(find_free_port())},
    )

    assert response.status_code == 400
    assert opener.opened() == []


def test_provider_relay_requires_the_chrome_session(tmp_path: Path) -> None:
    opener = RecordingSignInBrowsers()
    client, _app = _build_client(tmp_path, opener, is_authenticated=False)

    status, _body = _arm(client, _sign_in_url(find_free_port()))

    assert status == 401
    assert opener.opened() == []
