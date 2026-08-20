"""Witnessing tests for apps/minds/behaviors/browser-authorization/invariants.feature.

Each test carries a ``witnesses`` marker naming the invariant Rule (or child
Example) it verifies. The invariants are quantified over all routes and all
interleavings, so most markers carry a ``partial=`` note naming the residue
that no finite test can close; the tests here pin the observable core of each
property at the HTTP boundary of the browser authorization component.
"""

import time
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from itsdangerous import TimestampSigner
from itsdangerous import URLSafeTimedSerializer

from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import _COOKIE_MAX_AGE_SECONDS
from imbue.minds.desktop_client.cookie_manager import _COOKIE_SALT
from imbue.minds.desktop_client.cookie_manager import _SESSION_PAYLOAD
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.testing import tamper_session_cookie_signed_content
from imbue.minds.primitives import CookieSigningKey
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId

# A representative session-gated surface: the SPA index redirects any request
# without a valid session to /login, so its response is the observable verdict
# of "is this bearer authenticated?".
_GATED_ROUTE: str = "/ui/"


def _empty_client(tmp_path: Path, is_authenticated: bool = False) -> tuple[FlaskClient, FileAuthStore]:
    client, _app, auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )
    return client, auth_store


def _is_treated_as_unauthenticated(client: FlaskClient) -> bool:
    """Whether the gated SPA index turns this client away to the login page."""
    response = client.get(_GATED_ROUTE, follow_redirects=False)
    return response.status_code == 302 and response.headers["Location"] == "/login"


def _backdated_session_cookie(signing_key: CookieSigningKey, age_seconds: int) -> str:
    """A session cookie signed with ``signing_key`` but stamped ``age_seconds`` in the past.

    itsdangerous embeds the sign time in the token, so a backdated
    ``TimestampSigner`` mints a structurally valid cookie whose age the
    verifier can measure against the 30-day ceiling -- the only way to witness
    the expiry boundary without controlling the wall clock.
    """

    class _BackdatedSigner(TimestampSigner):
        def get_timestamp(self) -> int:
            return int(time.time()) - age_seconds

    serializer = URLSafeTimedSerializer(
        secret_key=signing_key.get_secret_value(), salt=_COOKIE_SALT, signer=_BackdatedSigner
    )
    return serializer.dumps(_SESSION_PAYLOAD)


@pytest.mark.witnesses(
    "browser-authorization.fetch-never-spends",
    partial="witnesses only the /login authentication URL, not every URL the system hands out",
)
def test_fetching_the_login_url_does_not_spend_the_code(tmp_path: Path) -> None:
    """A plain GET of the login URL (no script execution) leaves the code spendable."""
    client, auth_store = _empty_client(tmp_path)
    code = OneTimeCode("fetch-inert-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    fetched = client.get("/login", query_string={"one_time_code": str(code)}, follow_redirects=False)
    assert fetched.status_code == 200
    # Merely fetching establishes no session: the page is inert HTML, not a Set-Cookie.
    assert not any(SESSION_COOKIE_NAME in header for header in fetched.headers.getlist("Set-Cookie"))

    # The code survived the fetch: executing the authentication step still spends it.
    authenticated = client.get("/authenticate", query_string={"one_time_code": str(code)}, follow_redirects=False)
    assert authenticated.status_code == 307
    assert any(SESSION_COOKIE_NAME in header for header in authenticated.headers.getlist("Set-Cookie"))


@pytest.mark.witnesses("browser-authorization.unauthenticated-home")
@pytest.mark.witnesses(
    "browser-authorization.no-data-without-session",
    partial="witnesses the '/' surface only; the absence of user data across every route is universally quantified",
)
def test_unauthenticated_home_shows_auth_prompt_and_hides_workspaces(tmp_path: Path) -> None:
    """A visitor with no session at "/" is sent to the auth prompt and sees no workspace."""
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=False, backend_resolver=resolver
    )

    home = client.get("/", follow_redirects=False)
    # Sent to authenticate, revealing nothing about the installation's workspaces.
    assert home.status_code == 302
    assert home.headers["Location"] == "/login"
    assert str(agent_id) not in home.get_data(as_text=True)

    # The authentication prompt points at the login URL printed in the terminal
    # and is itself user-independent.
    prompt = client.get("/login", follow_redirects=False)
    assert prompt.status_code == 200
    prompt_body = prompt.get_data(as_text=True)
    assert "one-time login link" in prompt_body
    assert "terminal" in prompt_body
    assert str(agent_id) not in prompt_body


@pytest.mark.witnesses("browser-authorization.unauthenticated-arrival")
def test_unauthenticated_arrival_at_post_login_is_sent_to_authenticate_not_a_destination(tmp_path: Path) -> None:
    """An arrival at "/post-login" with no session goes to authenticate, even with a destination asked."""
    client, _auth_store = _empty_client(tmp_path)

    # A caller-supplied destination is not honored while unauthenticated: the
    # gate wins and the bearer is sent to authenticate, not to /create.
    response = client.get("/post-login", query_string={"return_to": "/create"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"] == "/login"


@pytest.mark.witnesses(
    "browser-authorization.sessions-unforgeable",
    partial="witnesses one tampering; that *any* alteration invalidates the cookie is universally quantified",
)
def test_tampered_session_cookie_is_treated_as_unauthenticated(tmp_path: Path) -> None:
    client, auth_store = _empty_client(tmp_path)
    valid_cookie = create_session_cookie(signing_key=auth_store.get_signing_key())
    tampered = tamper_session_cookie_signed_content(valid_cookie)

    client.set_cookie(SESSION_COOKIE_NAME, tampered)
    assert _is_treated_as_unauthenticated(client)


@pytest.mark.witnesses(
    "browser-authorization.sessions-unforgeable",
    partial="witnesses the cross-installation clause; unforgeability over all keys/interleavings is unbounded",
)
def test_foreign_installation_cookie_is_treated_as_unauthenticated(tmp_path: Path) -> None:
    client, _auth_store = _empty_client(tmp_path)
    # A cookie minted by a *different* data directory carries a different
    # signing key, so this installation must reject it.
    other_installation = FileAuthStore(data_directory=tmp_path / "other-installation")
    foreign_cookie = create_session_cookie(signing_key=other_installation.get_signing_key())

    client.set_cookie(SESSION_COOKIE_NAME, foreign_cookie)
    assert _is_treated_as_unauthenticated(client)


@pytest.mark.witnesses(
    "browser-authorization.sessions-unforgeable",
    partial="witnesses the 30-day ceiling; the boundary is a single point, not the whole time axis",
)
def test_expired_session_cookie_is_treated_as_unauthenticated(tmp_path: Path) -> None:
    client, auth_store = _empty_client(tmp_path)
    signing_key = auth_store.get_signing_key()

    # Just inside the ceiling still authenticates; just past it does not.
    fresh_cookie = _backdated_session_cookie(signing_key, age_seconds=_COOKIE_MAX_AGE_SECONDS - 3600)
    client.set_cookie(SESSION_COOKIE_NAME, fresh_cookie)
    assert not _is_treated_as_unauthenticated(client)

    expired_cookie = _backdated_session_cookie(signing_key, age_seconds=_COOKIE_MAX_AGE_SECONDS + 3600)
    client.set_cookie(SESSION_COOKIE_NAME, expired_cookie)
    assert _is_treated_as_unauthenticated(client)


def _bridging_client(tmp_path: Path, is_authenticated: bool) -> FlaskClient:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_port=8421,
        mngr_forward_browser_bridge_token="bridge-secret-token",
    )
    return client


@pytest.mark.witnesses(
    "browser-authorization.single-credential",
    partial="witnesses the workspace bridge route; that no route ever demands a second credential is universal",
)
def test_authenticated_session_alone_reaches_a_workspace_without_a_second_credential(tmp_path: Path) -> None:
    """The session cookie alone bridges an authenticated browser toward a workspace.

    No imbue-cloud account sign-in or any other credential is presented -- the
    only thing distinguishing the two clients below is the session cookie.
    """
    # Without the session, the workspace bridge refuses and sends the browser to authenticate.
    unauthenticated = _bridging_client(tmp_path, is_authenticated=False)
    turned_away = unauthenticated.get("/forward-bridge", query_string={"next": "/goto/host/"}, follow_redirects=False)
    assert turned_away.status_code == 302
    assert turned_away.headers["Location"] == "/"

    # With only the session cookie, the same request is bridged onward to the workspace origin.
    authenticated = _bridging_client(tmp_path, is_authenticated=True)
    bridged = authenticated.get("/forward-bridge", query_string={"next": "/goto/host/"}, follow_redirects=False)
    assert bridged.status_code == 302
    assert bridged.headers["Location"].startswith("https://localhost:8421/_bridge")
    assert "next=%2Fgoto%2Fhost%2F" in bridged.headers["Location"]


@pytest.mark.witnesses(
    "browser-authorization.no-open-redirects",
    partial="witnesses the forward-bridge route; the blanket 'no open redirects anywhere' is universally quantified",
)
def test_off_origin_forward_bridge_destination_is_confined_to_the_origin(tmp_path: Path) -> None:
    """A protocol-relative "next" at /forward-bridge is dropped for the default same-origin path."""
    client = _bridging_client(tmp_path, is_authenticated=True)

    response = client.get("/forward-bridge", query_string={"next": "//evil.com/x"}, follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["Location"]
    # The off-origin destination is ignored; the confined default "/" rides through instead.
    assert "evil.com" not in location
    assert "next=%2F&" in location or location.endswith("next=%2F")
