import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlencode

import httpx
import pytest

from imbue.minds.desktop_client.mock_provider_relay_test import RecordingSignInCallbackForwarder
from imbue.minds.desktop_client.provider_relay import InvalidSignInUrlError
from imbue.minds.desktop_client.provider_relay import ProviderRelayRegistry
from imbue.minds.desktop_client.provider_relay import ProviderSignInRelay
from imbue.minds.desktop_client.provider_relay import RelayArmResult
from imbue.minds.desktop_client.provider_relay import RelayRequestVerdict
from imbue.minds.desktop_client.provider_relay import SignInCallback
from imbue.minds.desktop_client.provider_relay import SignInCallbackRefusedError
from imbue.minds.desktop_client.provider_relay import SignInCallbackResult
from imbue.minds.desktop_client.provider_relay import SignInForwardError
from imbue.minds.desktop_client.provider_relay import SignInOutcome
from imbue.minds.desktop_client.provider_relay import WorkspaceChatSignInForwarder
from imbue.minds.desktop_client.provider_relay import decide_relay_request
from imbue.minds.desktop_client.provider_relay import page_for_result
from imbue.minds.desktop_client.provider_relay import parse_provider_sign_in_url
from imbue.minds.desktop_client.testing import scripted_chat_callback_server
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import find_free_port

_STATE = "state-8f3d61c2"


def _sign_in_url(
    *,
    origin: str = "https://claude.ai/oauth/authorize",
    redirect_uri: str | None = "http://localhost:54871/callback",
    state: str | None = _STATE,
) -> str:
    query = {"client_id": "client-40c1", "response_type": "code", "code_challenge": "challenge-77e0"}
    if redirect_uri is not None:
        query["redirect_uri"] = redirect_uri
    if state is not None:
        query["state"] = state
    return f"{origin}?{urlencode(query)}"


@pytest.mark.parametrize(
    ("origin", "redirect_uri", "expected"),
    [
        pytest.param(
            "https://claude.ai/oauth/authorize",
            "http://localhost:54871/callback",
            SignInCallback(port=54871, path="/callback", state=_STATE),
            id="claude-subscription",
        ),
        pytest.param(
            "https://platform.claude.com/oauth/authorize",
            "http://127.0.0.1:32768/callback",
            SignInCallback(port=32768, path="/callback", state=_STATE),
            id="claude-console-linux-ephemeral-port",
        ),
        pytest.param(
            "https://claude.ai/oauth/authorize",
            "http://localhost:16000/callback",
            SignInCallback(port=16000, path="/callback", state=_STATE),
            id="claude-lowest-gvisor-ephemeral-port",
        ),
        pytest.param(
            "https://claude.com/cai/oauth/authorize",
            "http://localhost:65535/callback",
            SignInCallback(port=65535, path="/callback", state=_STATE),
            id="claude-com-highest-port",
        ),
        pytest.param(
            "https://auth.openai.com/oauth/authorize",
            "http://localhost:1455/auth/callback",
            SignInCallback(port=1455, path="/auth/callback", state=_STATE),
            id="chatgpt",
        ),
        pytest.param(
            "https://auth.openai.com/oauth/authorize",
            "http://localhost:1457/auth/callback",
            SignInCallback(port=1457, path="/auth/callback", state=_STATE),
            id="chatgpt-fallback-port",
        ),
    ],
)
def test_parse_provider_sign_in_url_returns_the_loopback_callback(
    origin: str, redirect_uri: str, expected: SignInCallback
) -> None:
    assert parse_provider_sign_in_url(_sign_in_url(origin=origin, redirect_uri=redirect_uri)) == expected


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(_sign_in_url(origin="http://claude.ai/oauth/authorize"), id="not-https"),
        pytest.param(_sign_in_url(origin="https://evil.example/oauth/authorize"), id="unknown-host"),
        pytest.param(_sign_in_url(origin="https://claude.ai.evil.example/oauth/authorize"), id="lookalike-host"),
        pytest.param(_sign_in_url(origin="https://evil.example\\@claude.ai/oauth/authorize"), id="backslash-userinfo"),
        pytest.param(_sign_in_url(origin="https://evil.example@claude.ai/oauth/authorize"), id="userinfo"),
        pytest.param(_sign_in_url(origin="https://claude.ai:8443/oauth/authorize"), id="explicit-port"),
        pytest.param(_sign_in_url(origin="https://CLAUDE.ai/oauth/authorize"), id="uppercase-host"),
        pytest.param(_sign_in_url(origin="https://claude.ai/oauth/\tauthorize"), id="control-character"),
        pytest.param(_sign_in_url(redirect_uri=None), id="no-redirect-uri"),
        pytest.param(_sign_in_url(state=None), id="no-state"),
        pytest.param(_sign_in_url(state=""), id="empty-state"),
        pytest.param(_sign_in_url(redirect_uri="https://localhost:54871/callback"), id="redirect-not-http"),
        pytest.param(_sign_in_url(redirect_uri="http://example.com:54871/callback"), id="redirect-not-loopback"),
        pytest.param(_sign_in_url(redirect_uri="http://[::1]:54871/callback"), id="redirect-ipv6-literal"),
        pytest.param(_sign_in_url(redirect_uri="http://evil@localhost:54871/callback"), id="redirect-userinfo"),
        pytest.param(_sign_in_url(redirect_uri="http://localhost/callback"), id="redirect-no-port"),
        pytest.param(_sign_in_url(redirect_uri="http://localhost:5432/callback"), id="redirect-below-ephemeral-range"),
        pytest.param(
            _sign_in_url(redirect_uri="http://localhost:15999/callback"), id="redirect-just-below-ephemeral-range"
        ),
        pytest.param(_sign_in_url(redirect_uri="http://localhost:65536/callback"), id="redirect-port-out-of-range"),
        pytest.param(_sign_in_url(redirect_uri="http://localhost:54871/auth/callback"), id="codex-path-other-port"),
        pytest.param(_sign_in_url(redirect_uri="http://localhost:54871/steal"), id="redirect-unknown-path"),
    ],
)
def test_parse_provider_sign_in_url_rejects_anything_but_a_provider_with_a_loopback_callback(url: str) -> None:
    with pytest.raises(InvalidSignInUrlError):
        parse_provider_sign_in_url(url)


def test_parse_provider_sign_in_url_rejects_a_repeated_state() -> None:
    url = _sign_in_url() + "&" + urlencode({"state": "state-other-2b19"})

    with pytest.raises(InvalidSignInUrlError, match="exactly one"):
        parse_provider_sign_in_url(url)


_CALLBACK = SignInCallback(port=54871, path="/callback", state=_STATE)


@pytest.mark.parametrize(
    ("method", "path_and_query", "is_callback_taken", "expected"),
    [
        pytest.param(
            "GET", f"/callback?code=c-1&state={_STATE}", False, RelayRequestVerdict.FORWARD_CALLBACK, id="callback"
        ),
        pytest.param(
            "GET",
            f"/callback?error=access_denied&state={_STATE}",
            False,
            RelayRequestVerdict.FORWARD_CALLBACK,
            id="denied",
        ),
        pytest.param(
            "POST", f"/callback?code=c-1&state={_STATE}", False, RelayRequestVerdict.REJECT_METHOD, id="not-get"
        ),
        pytest.param(
            "GET", f"/auth/callback?code=c-1&state={_STATE}", False, RelayRequestVerdict.NOT_FOUND, id="wrong-path"
        ),
        pytest.param(
            "GET", "/callback?code=c-1&state=state-forged-0a", False, RelayRequestVerdict.STALE_STATE, id="wrong-state"
        ),
        pytest.param("GET", "/callback?code=c-1", False, RelayRequestVerdict.STALE_STATE, id="no-state"),
        pytest.param(
            "GET", f"/callback?state={_STATE}&state={_STATE}", False, RelayRequestVerdict.STALE_STATE, id="state-twice"
        ),
        pytest.param(
            "GET", f"/callback?code=c-1&state={_STATE}", True, RelayRequestVerdict.ALREADY_HANDLED, id="reload"
        ),
        pytest.param("GET", "/favicon.ico", True, RelayRequestVerdict.NOT_FOUND, id="other-path-after-callback"),
    ],
)
def test_decide_relay_request(
    method: str, path_and_query: str, is_callback_taken: bool, expected: RelayRequestVerdict
) -> None:
    assert decide_relay_request(method, path_and_query, _CALLBACK, is_callback_taken) == expected


def test_page_for_a_failed_sign_in_names_the_provider_and_keeps_the_workspaces_reason_short() -> None:
    page = page_for_result(
        SignInCallbackResult(outcome=SignInOutcome.FAILED, provider_name="OpenAI", detail="x" * 5000)
    )

    assert page.title == "OpenAI sign-in didn't finish"
    assert page.message == "x" * 300 + " Go back to Imbue Studio to try again, or to sign in another way."


_SIGNED_IN = SignInCallbackResult(outcome=SignInOutcome.SIGNED_IN, provider_name="Anthropic")
_WORKSPACE_ID = AgentId("agent-" + "9a" * 16)
_OTHER_WORKSPACE_ID = AgentId("agent-" + "3e" * 16)
_FLOW_ID = "0f5d2c7e8a1b4c3d9e6f7a8b9c0d1e2f"


def _relay(
    forwarder: RecordingSignInCallbackForwarder,
    handled: threading.Event,
    port: int | None = None,
    workspace_id: AgentId = _WORKSPACE_ID,
    flow_id: str = _FLOW_ID,
    no_callback_timeout_seconds: float = 30.0,
    after_callback_seconds: float = 30.0,
) -> ProviderSignInRelay:
    return ProviderSignInRelay(
        workspace_id=workspace_id,
        flow_id=flow_id,
        callback=SignInCallback(port=port or find_free_port(), path="/callback", state=_STATE),
        forwarder=forwarder,
        on_callback_handled=handled.set,
        no_callback_timeout_seconds=no_callback_timeout_seconds,
        after_callback_seconds=after_callback_seconds,
    )


@contextmanager
def _armed_relay(
    forwarder: RecordingSignInCallbackForwarder,
    handled: threading.Event,
    no_callback_timeout_seconds: float = 30.0,
    after_callback_seconds: float = 30.0,
) -> Iterator[ProviderSignInRelay]:
    relay = _relay(
        forwarder,
        handled,
        no_callback_timeout_seconds=no_callback_timeout_seconds,
        after_callback_seconds=after_callback_seconds,
    )
    assert relay.start() is True
    try:
        yield relay
    finally:
        relay.stop()


def _get(relay: ProviderSignInRelay, path_and_query: str) -> httpx.Response:
    return httpx.get(f"http://127.0.0.1:{relay.callback.port}{path_and_query}", timeout=10.0, follow_redirects=False)


def test_relay_refuses_everything_but_the_callback_and_forwards_nothing() -> None:
    forwarder = RecordingSignInCallbackForwarder(result=_SIGNED_IN)
    handled = threading.Event()
    with _armed_relay(forwarder, handled) as relay:
        wrong_path = _get(relay, f"/auth/callback?code=c-1&state={_STATE}")
        stale = _get(relay, "/callback?code=c-1&state=state-forged-0a")
        posted = httpx.post(f"http://127.0.0.1:{relay.callback.port}/callback?state={_STATE}", timeout=10.0)

    assert [wrong_path.status_code, stale.status_code, posted.status_code] == [404, 400, 405]
    assert "This sign-in page is out of date" in stale.text
    assert forwarder.forwarded() == []
    assert not handled.is_set()


def test_relay_answers_the_callback_with_its_own_page_saying_how_the_sign_in_ended() -> None:
    forwarder = RecordingSignInCallbackForwarder(result=_SIGNED_IN)
    handled = threading.Event()
    with _armed_relay(forwarder, handled) as relay:
        callback = _get(relay, f"/callback?code=c-1&state={_STATE}")

    assert callback.status_code == 200
    assert "You&#x27;re signed in to Anthropic" in callback.text
    assert callback.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert forwarder.forwarded() == [f"/callback?code=c-1&state={_STATE}"]
    assert handled.is_set()


def test_relay_escapes_the_workspaces_reason_for_a_failure() -> None:
    result = SignInCallbackResult(
        outcome=SignInOutcome.FAILED, provider_name="Anthropic", detail="<script>alert(1)</script>Denied."
    )
    forwarder = RecordingSignInCallbackForwarder(result=result)
    with _armed_relay(forwarder, threading.Event()) as relay:
        callback = _get(relay, f"/callback?error=access_denied&state={_STATE}")

    assert "Anthropic sign-in didn&#x27;t finish" in callback.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;Denied." in callback.text
    assert "<script>" not in callback.text


def test_relay_passes_the_callback_on_once_even_when_the_browser_sends_it_again() -> None:
    forwarder = RecordingSignInCallbackForwarder(result=_SIGNED_IN, is_holding=True)
    with _armed_relay(forwarder, threading.Event()) as relay:
        first: list[httpx.Response] = []
        in_flight = threading.Thread(target=lambda: first.append(_get(relay, f"/callback?code=c-1&state={_STATE}")))
        in_flight.start()
        try:
            assert poll_until(lambda: forwarder.forwarded() != [], timeout=10.0, poll_interval=0.01)
            duplicate = _get(relay, f"/callback?code=c-1&state={_STATE}")
        finally:
            forwarder.release_held()
            in_flight.join(timeout=10.0)
        reload = _get(relay, f"/callback?code=c-1&state={_STATE}")

    assert "This sign-in has already gone through" in duplicate.text
    assert "This sign-in has already gone through" in reload.text
    assert "You&#x27;re signed in" in first[0].text
    assert len(forwarder.forwarded()) == 1


def test_relay_keeps_waiting_when_the_callback_cannot_reach_the_workspace_and_a_reload_retries() -> None:
    forwarder = RecordingSignInCallbackForwarder(result=_SIGNED_IN, is_failing=True)
    handled = threading.Event()
    with _armed_relay(forwarder, handled) as relay:
        unreachable = _get(relay, f"/callback?code=c-1&state={_STATE}")
        is_still_listening = not relay.is_stopped()
        forwarder.is_failing = False
        retried = _get(relay, f"/callback?code=c-1&state={_STATE}")

    assert unreachable.status_code == 502
    assert "Couldn&#x27;t reach your machine" in unreachable.text
    assert is_still_listening
    assert "You&#x27;re signed in to Anthropic" in retried.text
    assert len(forwarder.forwarded()) == 2
    assert handled.is_set()


def test_relay_tells_the_browser_when_the_workspace_no_longer_takes_the_callback() -> None:
    forwarder = RecordingSignInCallbackForwarder(result=_SIGNED_IN, is_refusing=True)
    handled = threading.Event()
    with _armed_relay(forwarder, handled) as relay:
        refused = _get(relay, f"/callback?code=c-1&state={_STATE}")
        reload = _get(relay, f"/callback?code=c-1&state={_STATE}")

    assert refused.status_code == 409
    assert "This sign-in is no longer waiting" in refused.text
    assert "This sign-in has already gone through" in reload.text
    assert len(forwarder.forwarded()) == 1
    assert not handled.is_set()


def test_relay_gives_the_port_up_when_no_callback_arrives_in_time() -> None:
    forwarder = RecordingSignInCallbackForwarder(result=_SIGNED_IN)
    with _armed_relay(forwarder, threading.Event(), no_callback_timeout_seconds=0.3) as relay:
        assert relay.wait_until_stopped(timeout_seconds=10.0)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", relay.callback.port))


def test_relay_gives_the_port_up_soon_after_the_callback() -> None:
    forwarder = RecordingSignInCallbackForwarder(result=_SIGNED_IN)
    with _armed_relay(forwarder, threading.Event(), after_callback_seconds=0.3) as relay:
        _get(relay, f"/callback?code=c-1&state={_STATE}")

        assert relay.wait_until_stopped(timeout_seconds=10.0)


@pytest.mark.parametrize("occupant_address", ["127.0.0.1", "0.0.0.0"])
def test_relay_will_not_listen_on_a_port_another_program_listens_on(occupant_address: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant:
        occupant.bind((occupant_address, 0))
        occupant.listen()
        relay = _relay(
            RecordingSignInCallbackForwarder(result=_SIGNED_IN), threading.Event(), port=occupant.getsockname()[1]
        )

        assert relay.start() is False


def test_registry_reuses_a_workspaces_relay_for_the_same_sign_in() -> None:
    registry = ProviderRelayRegistry()
    first = _relay(RecordingSignInCallbackForwarder(result=_SIGNED_IN), threading.Event())
    again = _relay(RecordingSignInCallbackForwarder(result=_SIGNED_IN), threading.Event(), port=first.callback.port)
    try:
        results = [registry.arm(first), registry.arm(again)]
        is_first_listening = not first.is_stopped()
    finally:
        registry.stop_all()

    assert results == [RelayArmResult.ARMED, RelayArmResult.ALREADY_ARMED]
    assert is_first_listening


def test_registry_never_hands_one_workspaces_port_to_another() -> None:
    registry = ProviderRelayRegistry()
    first = _relay(RecordingSignInCallbackForwarder(result=_SIGNED_IN), threading.Event())
    intruder = _relay(
        RecordingSignInCallbackForwarder(result=_SIGNED_IN),
        threading.Event(),
        port=first.callback.port,
        workspace_id=_OTHER_WORKSPACE_ID,
    )
    try:
        results = [registry.arm(first), registry.arm(intruder)]
        is_first_listening = not first.is_stopped()
    finally:
        registry.stop_all()

    assert results == [RelayArmResult.ARMED, RelayArmResult.PORT_UNAVAILABLE]
    assert is_first_listening


def test_registry_gives_a_port_whose_callback_was_answered_to_another_workspaces_sign_in() -> None:
    registry = ProviderRelayRegistry()
    finished = _relay(RecordingSignInCallbackForwarder(result=_SIGNED_IN), threading.Event())
    newcomer = _relay(
        RecordingSignInCallbackForwarder(result=_SIGNED_IN),
        threading.Event(),
        port=finished.callback.port,
        workspace_id=_OTHER_WORKSPACE_ID,
    )
    try:
        registry.arm(finished)
        _get(finished, f"/callback?code=c-1&state={_STATE}")
        result = registry.arm(newcomer)
        is_finished_stopped = finished.is_stopped()
        is_newcomer_listening = not newcomer.is_stopped()
    finally:
        registry.stop_all()

    assert result is RelayArmResult.ARMED
    assert is_finished_stopped
    assert is_newcomer_listening


def test_registry_replaces_a_workspaces_earlier_sign_in_and_disarms_only_the_current_one() -> None:
    registry = ProviderRelayRegistry()
    earlier = _relay(RecordingSignInCallbackForwarder(result=_SIGNED_IN), threading.Event(), flow_id="flow-earlier-51")
    later = _relay(RecordingSignInCallbackForwarder(result=_SIGNED_IN), threading.Event(), flow_id="flow-later-62")
    try:
        results = [registry.arm(earlier), registry.arm(later)]
        is_earlier_stopped = earlier.is_stopped()
        registry.disarm(_WORKSPACE_ID, "flow-earlier-51")
        is_later_listening_after_stale_disarm = not later.is_stopped()
        registry.disarm(_WORKSPACE_ID, "flow-later-62")
    finally:
        registry.stop_all()

    assert results == [RelayArmResult.ARMED, RelayArmResult.ARMED]
    assert is_earlier_stopped
    assert is_later_listening_after_stale_disarm
    assert later.is_stopped()


def test_registry_leaves_a_relay_that_took_its_callback_to_answer_reloads() -> None:
    registry = ProviderRelayRegistry()
    relay = _relay(RecordingSignInCallbackForwarder(result=_SIGNED_IN), threading.Event())
    try:
        registry.arm(relay)
        _get(relay, f"/callback?code=c-1&state={_STATE}")
        registry.disarm(_WORKSPACE_ID, _FLOW_ID)
        reload = _get(relay, f"/callback?code=c-1&state={_STATE}")
    finally:
        registry.stop_all()

    assert "This sign-in has already gone through" in reload.text


def test_registry_rate_limits_opening_sign_in_pages_per_workspace() -> None:
    registry = ProviderRelayRegistry()

    claims = [
        registry.claim_browser_open(_WORKSPACE_ID),
        registry.claim_browser_open(_WORKSPACE_ID),
        registry.claim_browser_open(_OTHER_WORKSPACE_ID),
    ]

    assert claims == [True, False, True]


def _forwarder(port: int) -> WorkspaceChatSignInForwarder:
    return WorkspaceChatSignInForwarder(
        mngr_forward_port=port,
        preauth_cookie="preauth-7d20",
        workspace_id=_WORKSPACE_ID,
        chat_service_label="chat-k3m9",
        flow_id=_FLOW_ID,
    )


def test_workspace_forwarder_posts_the_callback_to_the_chat_app_as_the_owner_and_reads_how_the_sign_in_ended() -> None:
    answer = {
        "state": "failed",
        "detail": "You didn't approve access.",
        "account_id": None,
        "provider_name": "Anthropic",
    }
    with scripted_chat_callback_server(200, json.dumps(answer).encode()) as (port, recorded):
        result = _forwarder(port).forward(f"/callback?error=access_denied&state={_STATE}")

    assert result == SignInCallbackResult(
        outcome=SignInOutcome.FAILED, provider_name="Anthropic", detail="You didn't approve access."
    )
    assert [(request.path, request.host) for request in recorded] == [
        (f"/api/accounts/flow/{_FLOW_ID}/callback", f"chat-k3m9.{_WORKSPACE_ID}.localhost")
    ]
    assert "mngr_forward_session=preauth-7d20" in recorded[0].cookie
    assert json.loads(recorded[0].body) == {"path_and_query": f"/callback?error=access_denied&state={_STATE}"}


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(403, b'{"error": "Only the owner"}', id="refused"),
        pytest.param(200, b"not json", id="unreadable"),
        pytest.param(200, b'{"state": "exploded", "provider_name": "Anthropic"}', id="unknown-state"),
    ],
)
def test_workspace_forwarder_raises_when_the_chat_app_does_not_answer_the_callback(status: int, body: bytes) -> None:
    with scripted_chat_callback_server(status, body) as (port, _recorded):
        with pytest.raises(SignInForwardError):
            _forwarder(port).forward("/callback")


def test_workspace_forwarder_reports_a_callback_the_chat_app_no_longer_takes() -> None:
    body = b'{"detail": "that sign-in is no longer active"}'
    with scripted_chat_callback_server(409, body) as (port, _recorded):
        with pytest.raises(SignInCallbackRefusedError, match="no longer active"):
            _forwarder(port).forward("/callback")


def test_workspace_forwarder_raises_when_the_workspace_is_unreachable() -> None:
    with pytest.raises(SignInForwardError, match="could not be reached"):
        _forwarder(find_free_port()).forward("/callback")
