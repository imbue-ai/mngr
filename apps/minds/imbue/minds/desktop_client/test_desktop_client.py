import gzip
import json
import os
import queue
import subprocess
import time
from collections.abc import Mapping
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx
import pytest
from flask import Response
from flask.testing import FlaskClient
from itsdangerous import TimestampSigner
from itsdangerous import URLSafeTimedSerializer
from pydantic import SecretStr
from pydantic import TypeAdapter
from sentry_sdk.types import Event
from werkzeug.test import TestResponse

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.sentry.core import COMPRESSED_LOG_EXTENSION
from imbue.imbue_common.sentry.core import ErrorAttachmentsS3Uploader
from imbue.imbue_common.sentry.s3_uploader import EXTRAS_UPLOADED_FILES_KEY
from imbue.imbue_common.sentry.testing import TEST_S3_BUCKET
from imbue.imbue_common.sentry.testing import capturing_sentry_client
from imbue.imbue_common.sentry.testing import recording_s3_bucket
from imbue.imbue_common.sentry.testing import registered_attachments_uploader
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import _build_requests_payload
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.app import _collect_remote_workspace_tiles
from imbue.minds.desktop_client.app import _finalize_and_mark_destroying
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.conftest import DEFAULT_SERVICE_NAME
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.console_log_staging import ELECTRON_CONSOLE_TAIL_FILENAME
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import _COOKIE_MAX_AGE_SECONDS
from imbue.minds.desktop_client.cookie_manager import _COOKIE_SALT
from imbue.minds.desktop_client.cookie_manager import _SESSION_PAYLOAD
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.data_types import BackupAccessState
from imbue.minds.desktop_client.data_types import RemoteWorkspaceKind
from imbue.minds.desktop_client.dek_store import bundle_mirror_path
from imbue.minds.desktop_client.dek_store import ensure_dek
from imbue.minds.desktop_client.dek_store import is_account_unlocked
from imbue.minds.desktop_client.dek_store import set_master_password_for_account
from imbue.minds.desktop_client.dek_store import verify_master_password_for_account
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import create_request_response_event
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import StaticPendingRequests
from imbue.minds.desktop_client.testing import blocking_release_wait_body
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import create_predefined_permission_request
from imbue.minds.desktop_client.testing import drain_ui_channel_frames
from imbue.minds.desktop_client.testing import exec_json_envelope
from imbue.minds.desktop_client.testing import install_stub_mngr_on_path
from imbue.minds.desktop_client.testing import record_provider_discovery_error
from imbue.minds.desktop_client.testing import tamper_session_cookie_signed_content
from imbue.minds.desktop_client.testing import write_stub_mngr
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceSecretsPayload
from imbue.minds.desktop_client.workspace_record_store import encode_encrypted_secrets
from imbue.minds.mngr_settings.provider_blocks import imbue_cloud_provider_name_for_account
from imbue.minds.primitives import CookieSigningKey
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import ServiceName
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import wait_for


def _create_test_desktop_client(
    tmp_path: Path,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.Client | None,
    agent_creator: AgentCreator | None = None,
    minds_config: MindsConfig | None = None,
) -> tuple[FlaskClient, FileAuthStore]:
    """Create a desktop client with the given backend resolver.

    ``minds_config`` is only needed by tests that depend on the error-reporting
    consent gate (unset leaves the gate absent, which post-login treats the
    same as an answered consent question).
    """
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=http_client,
        agent_creator=agent_creator,
        minds_config=minds_config,
    )
    client = app.test_client()

    return client, auth_store


def _setup_test_server(
    tmp_path: Path,
    service_name: ServiceName = DEFAULT_SERVICE_NAME,
) -> tuple[FlaskClient, FileAuthStore, AgentId]:
    """Set up a desktop client with a test backend for proxy testing."""
    agent_id = AgentId()

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {str(service_name): "http://test-backend"}},
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    return client, auth_store, agent_id


def _authenticate_client(
    client: FlaskClient,
    auth_store: FileAuthStore,
) -> None:
    """Authenticate a test client by minting a signed session cookie and adding it to the jar.

    The production path (GET /authenticate?one_time_code=...) sets a host-only
    ``minds_session`` cookie on the bare origin (workspace subdomains get their
    own session via the forward server's /goto/ auth bridge, not this cookie).
    Setting the cookie directly on the jar skips the /authenticate round-trip
    and its one-time-code bookkeeping in tests that only care about being
    signed in. The server-side logic the test is exercising is independent of
    the Set-Cookie emission path; the bare presence/signature of the cookie is
    what ``_is_authenticated`` checks.
    """
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.set_cookie(SESSION_COOKIE_NAME, cookie_value)


@pytest.mark.witnesses(
    "browser-authorization.missing-code",
    partial="covers only the /authenticate example; the outline's /login example conflicts with the documented static explanation page (see behavior_problems)",
)
def test_authenticate_without_one_time_code_returns_422(tmp_path: Path) -> None:
    """A missing one_time_code is a 422, not a 500."""
    client, _, _ = _setup_test_server(tmp_path)
    response = client.get("/authenticate", follow_redirects=False)
    assert response.status_code == 422


@pytest.mark.witnesses(
    "browser-authorization.fresh-code",
    partial="drives only the /authenticate hop (not the /login open) and asserts the session cookie is set, not that the code becomes spent",
)
def test_authenticate_with_valid_code_sets_cookie_and_redirects(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("auth-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    response = client.get(
        "/authenticate",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert any(SESSION_COOKIE_NAME in header for header in response.headers.getlist("Set-Cookie"))


@pytest.mark.witnesses(
    "browser-authorization.fresh-code",
    partial="drives only the /authenticate hop (not the /login open) and asserts the landing redirect, not that the session is authenticated or the code becomes spent",
)
def test_authenticate_redirects_to_landing_page(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("auth-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    response = client.get(
        "/authenticate",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/"


@pytest.mark.witnesses("browser-authorization.unknown-code")
def test_authenticate_with_invalid_code_returns_403(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get(
        "/authenticate",
        query_string={"one_time_code": "bogus-code-82734"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "invalid or has already been used" in response.text
    # No session is established: the refusal sets no session cookie.
    assert not any(SESSION_COOKIE_NAME in header for header in response.headers.getlist("Set-Cookie"))


@pytest.mark.witnesses(
    "browser-authorization.single-use-codes",
    partial="witnesses one reuse over the /authenticate route; no interleaving or sequence is exhausted",
)
@pytest.mark.witnesses("browser-authorization.used-code")
def test_authenticate_code_cannot_be_reused(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("once-only-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    # Precondition: the code has already been used to authenticate a session.
    first_response = client.get(
        "/authenticate",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )
    assert first_response.status_code == 307

    # Presenting the same (now spent) code again is refused, with an explanation.
    second_response = client.get(
        "/authenticate",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )
    assert second_response.status_code == 403
    assert "invalid or has already been used" in second_response.text
    # No new session is established by the refused re-use.
    assert not any(SESSION_COOKIE_NAME in header for header in second_response.headers.getlist("Set-Cookie"))


@pytest.mark.witnesses("browser-authorization.fresh-code")
def test_opening_fresh_authentication_url_authenticates_the_session(tmp_path: Path) -> None:
    """Opening the printed authentication URL in a browser (the /login -> /authenticate
    hop) lands on "/", authenticates the session, and spends the one-time code."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )
    code = OneTimeCode("fresh-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    # A browser opens the printed authentication URL. /login itself does not
    # consume the code; its in-page script hands off to /authenticate.
    login_response = client.get("/login", query_string={"one_time_code": str(code)}, follow_redirects=False)
    assert login_response.status_code == 200
    assert "/authenticate?one_time_code=" in login_response.get_data(as_text=True)

    # The script's destination: /authenticate consumes the code and establishes
    # the session, landing the browser on the home page "/".
    authenticate_response = client.get(
        "/authenticate", query_string={"one_time_code": str(code)}, follow_redirects=False
    )
    assert authenticate_response.status_code == 307
    assert authenticate_response.headers["location"] == "/"
    assert any(SESSION_COOKIE_NAME in header for header in authenticate_response.headers.getlist("Set-Cookie"))

    # The session is now authenticated: the client's cookie jar carries the
    # session, so an auth-gated route no longer bounces to /login.
    post_login_response = client.get("/post-login", follow_redirects=False)
    assert post_login_response.status_code == 302
    assert post_login_response.headers["location"] != "/login"

    # The one-time code is now spent: presenting it again is refused.
    replay_response = client.get("/authenticate", query_string={"one_time_code": str(code)}, follow_redirects=False)
    assert replay_response.status_code == 403


@pytest.mark.witnesses("browser-authorization.prefetch")
def test_prefetching_login_url_does_not_spend_the_code(tmp_path: Path) -> None:
    """A prefetcher fetching /login without running its script must not consume the
    code; the user can still authenticate later by really opening the URL."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )
    code = OneTimeCode("prefetch-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    prefetch_response = client.get("/login", query_string={"one_time_code": str(code)}, follow_redirects=False)
    assert prefetch_response.status_code == 200
    assert not any(SESSION_COOKIE_NAME in header for header in prefetch_response.headers.getlist("Set-Cookie"))

    authenticate_response = client.get(
        "/authenticate", query_string={"one_time_code": str(code)}, follow_redirects=False
    )
    assert authenticate_response.status_code == 307
    assert any(SESSION_COOKIE_NAME in header for header in authenticate_response.headers.getlist("Set-Cookie"))


@pytest.mark.witnesses("home-page.default-destination")
def test_post_login_redirects_to_create_when_no_workspaces(tmp_path: Path) -> None:
    """With consent answered and no workspaces, post-login lands on "/" (the new-workspace form)."""
    consented_config = MindsConfig(data_dir=tmp_path)
    consented_config.set_error_reporting_consent_given(True)
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None, minds_config=consented_config
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


@pytest.mark.witnesses("home-page.default-destination")
def test_post_login_redirects_to_accounts_when_workspaces_exist(tmp_path: Path) -> None:
    """With consent answered and at least one workspace, post-login lands on the account-management page."""
    consented_config = MindsConfig(data_dir=tmp_path)
    consented_config.set_error_reporting_consent_given(True)
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {"web": "http://backend"}},
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None, minds_config=consented_config
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/accounts"


def test_post_login_redirects_to_login_when_unauthenticated(tmp_path: Path) -> None:
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


@pytest.mark.witnesses("home-page.safe-return-to")
def test_post_login_honors_safe_return_to(tmp_path: Path) -> None:
    """With consent answered, a same-origin ``return_to`` (e.g. /create) wins over the default destination."""
    consented_config = MindsConfig(data_dir=tmp_path)
    consented_config.set_error_reporting_consent_given(True)
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None, minds_config=consented_config
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", query_string={"return_to": "/create"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/create"


@pytest.mark.witnesses("browser-authorization.post-login-return-to-confined")
@pytest.mark.witnesses(
    "browser-authorization.no-open-redirects",
    partial="witnesses the /post-login route with one off-origin shape; the blanket property is universal",
)
@pytest.mark.witnesses(
    "home-page.default-destination",
    partial="covers only the rejected-as-unsafe return destination with no workspaces (the '/' row)",
)
def test_post_login_ignores_unsafe_return_to(tmp_path: Path) -> None:
    """With consent answered, an off-origin ``return_to`` is rejected and the default destination is used."""
    consented_config = MindsConfig(data_dir=tmp_path)
    consented_config.set_error_reporting_consent_given(True)
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None, minds_config=consented_config
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", query_string={"return_to": "https://evil.com"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


# ``/post-login`` is the observable "page that requires a session": with a valid
# session it routes the browser on to a destination (``/`` when there are no
# workspaces), and without one it bounces to ``/login`` to authenticate. So a
# ``location`` other than ``/login`` means "authenticated" and ``/login`` means
# "treated as unauthenticated" -- the shape every scenario below asserts on.
_UNAUTHENTICATED_LOCATION = "/login"


def _session_cookie_from_response(response: TestResponse) -> str:
    """Extract the ``minds_session`` cookie value from a response's Set-Cookie headers."""
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.getlist("Set-Cookie"):
        jar.load(header)
    return jar[SESSION_COOKIE_NAME].value


class _ExpiredTimestampSigner(TimestampSigner):
    """Signs exactly like the real cookie signer but stamps the token >30 days in the past.

    ``verify_session_cookie`` builds its own serializer with a fixed ``max_age`` of
    30 days, so a validly-signed cookie whose embedded timestamp is older than that
    is the only way to exercise the expiry path. Subclassing the signer (rather than
    patching ``time``) is the injection point itsdangerous gives for controlling the
    stamped time.
    """

    def get_timestamp(self) -> int:
        return int(time.time()) - _COOKIE_MAX_AGE_SECONDS - 24 * 60 * 60


def _make_expired_session_cookie(signing_key: CookieSigningKey) -> str:
    """Mint a validly-signed session cookie whose timestamp is older than 30 days."""
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value(), signer=_ExpiredTimestampSigner)
    return serializer.dumps(_SESSION_PAYLOAD, salt=_COOKIE_SALT)


@pytest.mark.witnesses("browser-authorization.survives-restart")
@pytest.mark.witnesses(
    "browser-authorization.signing-key-minted-once",
    partial="only that the persisted signing key keeps a live session valid across a restart; "
    "does not exercise concurrent first-mint agreement or the corrupted-key hard failure",
)
@pytest.mark.witnesses(
    "browser-authorization.single-use-codes",
    partial="only that a code spent before the restart stays spent afterwards; not the full "
    "no-interleaving-spends-twice quantifier",
)
def test_session_survives_desktop_client_restart(tmp_path: Path) -> None:
    """A session established before a restart still authenticates afterward, with no new code."""
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    # Given an authenticated user: authenticate for real against this data directory,
    # which mints and persists the signing key and consumes the one-time code.
    client_before, auth_store_before = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=resolver, http_client=None
    )
    code = OneTimeCode("restart-code-{}".format(AgentId()))
    auth_store_before.add_one_time_code(code=code)
    auth_response = client_before.get(
        "/authenticate", query_string={"one_time_code": str(code)}, follow_redirects=False
    )
    assert auth_response.status_code == 307
    session_cookie = _session_cookie_from_response(auth_response)

    # When the desktop client is stopped and started again: a fresh app + auth store
    # over the SAME data directory (a new FileAuthStore reading the persisted key).
    client_after, _auth_store_after = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=resolver, http_client=None
    )

    # And the user reloads the home page carrying the pre-restart cookie.
    client_after.set_cookie(SESSION_COOKIE_NAME, session_cookie)
    landing = client_after.get("/post-login", follow_redirects=False)

    # Then they are still authenticated (routed onward, not bounced to authenticate).
    assert landing.status_code == 302
    assert landing.headers["location"] != _UNAUTHENTICATED_LOCATION
    assert landing.headers["location"] == "/"

    # And they do not need a new one-time code: the only code ever minted is already
    # spent (replaying it is refused), yet the restored session authenticates on its own.
    replay = client_after.get("/authenticate", query_string={"one_time_code": str(code)}, follow_redirects=False)
    assert replay.status_code == 403


@pytest.mark.witnesses("browser-authorization.tampered-cookie")
@pytest.mark.witnesses(
    "browser-authorization.sessions-unforgeable",
    partial="only the signed-content-invalidates clause, observed at the /post-login gate",
)
def test_tampered_session_cookie_is_unauthenticated(tmp_path: Path) -> None:
    """A session cookie whose signed content is altered is treated as unauthenticated at the page gate."""
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=StaticBackendResolver(url_by_agent_and_service={}), http_client=None
    )
    valid_cookie = create_session_cookie(signing_key=auth_store.get_signing_key())
    tampered_cookie = tamper_session_cookie_signed_content(valid_cookie)
    client.set_cookie(SESSION_COOKIE_NAME, tampered_cookie)

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == _UNAUTHENTICATED_LOCATION


@pytest.mark.witnesses("browser-authorization.foreign-cookie")
@pytest.mark.witnesses(
    "browser-authorization.sessions-unforgeable",
    partial="only the cookies-from-another-installation-are-invalid clause, observed at the /post-login gate",
)
def test_foreign_installation_cookie_is_unauthenticated(tmp_path: Path) -> None:
    """A cookie signed by a different data directory's key is not accepted here."""
    this_client, this_auth_store = _create_test_desktop_client(
        tmp_path=tmp_path / "this",
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
    )
    # A second installation: a different data directory mints its own signing key.
    other_auth_store = FileAuthStore(data_directory=tmp_path / "other" / "auth")
    assert other_auth_store.get_signing_key() != this_auth_store.get_signing_key()

    foreign_cookie = create_session_cookie(signing_key=other_auth_store.get_signing_key())
    this_client.set_cookie(SESSION_COOKIE_NAME, foreign_cookie)

    response = this_client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == _UNAUTHENTICATED_LOCATION


@pytest.mark.witnesses("browser-authorization.expired-cookie")
@pytest.mark.witnesses(
    "browser-authorization.sessions-unforgeable",
    partial="only the cookies-older-than-30-days-are-invalid clause, observed at the /post-login gate",
)
def test_expired_session_cookie_is_unauthenticated(tmp_path: Path) -> None:
    """A validly-signed session cookie issued more than 30 days ago is treated as unauthenticated."""
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=StaticBackendResolver(url_by_agent_and_service={}), http_client=None
    )
    # Signed with this installation's own key: only the >30-day age, not the signature, is wrong.
    expired_cookie = _make_expired_session_cookie(signing_key=auth_store.get_signing_key())
    client.set_cookie(SESSION_COOKIE_NAME, expired_cookie)

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == _UNAUTHENTICATED_LOCATION


# -- Leased imbue_cloud host account-binding tests --


@pytest.mark.witnesses("browser-authorization.already-authenticated")
def test_login_redirects_if_already_authenticated(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    new_code = OneTimeCode("second-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=new_code)

    response = client.get(
        "/login",
        query_string={"one_time_code": str(new_code)},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == "/"
    assert auth_store.validate_and_consume_code(code=new_code) is True


def test_unhandled_exception_returns_500_with_message(tmp_path: Path) -> None:
    """Unhandled exceptions in routes produce a 500 response with the error message."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    @app.get("/explode")
    def explode() -> Response:
        raise RuntimeError("test boom")

    client = app.test_client()
    response = client.get("/explode")
    assert response.status_code == 500
    assert "test boom" in response.text


# -- Workspace-list / destroying-marker derivation helpers --


def test_build_workspace_list_returns_workspaces_for_the_channel(tmp_path: Path) -> None:
    """``_build_workspace_list`` surfaces each resolver-known workspace as a payload row.

    The rows it builds are what the ``workspaces`` channel message (and the
    bootstrap snapshot) are derived from.
    """
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {str(DEFAULT_SERVICE_NAME): "http://test-backend"}},
    )

    workspaces = _build_workspace_list(backend_resolver)
    assert len(workspaces) == 1
    assert workspaces[0]["id"] == str(agent_id)


def test_destroying_marker_includes_ids_with_live_destroy(tmp_path: Path) -> None:
    """An agent with an alive destroy pid + still in the resolver shows up as running.

    main.js keys its "ok to navigate the user away from this machine"
    decision off this list, so the helper must surface every in-flight or
    failed destroy id whose marker dir exists on disk.
    """
    agent_id = AgentId()
    paths = InstallationPaths(data_dir=tmp_path)
    destroying_dir = tmp_path / "destroying" / str(agent_id)
    destroying_dir.mkdir(parents=True)
    # The current process pid is alive, so the helper sees the destroy as
    # RUNNING (rather than DONE/FAILED, which would still be a valid hit but
    # the running case is the most direct check).
    (destroying_dir / "pid").write_text(str(os.getpid()))
    (destroying_dir / "output.log").write_text("destroy in flight...\n")

    # The pid is alive, so the record is RUNNING regardless of host state; an
    # empty resolver is enough to drive the helper.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    marker = _finalize_and_mark_destroying(paths, backend_resolver, None, None)
    assert marker == {str(agent_id): "running"}


def test_destroying_marker_returns_empty_when_paths_is_none() -> None:
    """The test-server helper builds a minimal app without InstallationPaths;
    the helper must tolerate that without raising."""
    assert _finalize_and_mark_destroying(None, StaticBackendResolver(url_by_agent_and_service={}), None, None) == {}


def _write_dead_destroy_dir(paths: InstallationPaths, agent_id: AgentId, host_id: HostId) -> None:
    """Create a destroying/<agent_id>/ dir whose wrapper pid is already dead.

    Spawns and reaps a trivial child so its pid is reliably not alive, then
    writes a legacy-shaped destroy marker (pid, host_id, log -- no ``provider``
    file, which ``start_destroy`` also writes when discovery knows the owning
    provider), so status reads take the legacy absence-equals-gone path.
    """
    dir_path = paths.data_dir / "destroying" / str(agent_id)
    dir_path.mkdir(parents=True)
    proc = subprocess.Popen(["true"])
    proc.wait()
    (dir_path / "pid").write_text(f"{proc.pid}\n")
    (dir_path / "host_id").write_text(f"{host_id}\n")
    (dir_path / "output.log").write_text("done\n")


def test_finalize_and_mark_destroying_finalizes_when_host_gone(tmp_path: Path) -> None:
    """A finished destroy whose host is gone is DONE: the record is tombstoned.

    Finalization happens only once the host is actually gone, not
    synchronously on click. The record is kept (state=DESTROYED, secrets
    intact) so the machine's backups stay reachable, but it no longer
    reads as the machine's owner.
    """
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    _write_dead_destroy_dir(paths, agent_id, HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(HostId.generate()),
        display_name="doomed",
        color=None,
        is_cloud_row=False,
    )
    # Resolver knows no active agents and reports no host state -> the host is
    # gone -> the destroy is DONE.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    marker = _finalize_and_mark_destroying(paths, backend_resolver, session_store, cli)

    assert marker == {}
    assert not (paths.data_dir / "destroying" / str(agent_id)).exists()
    assert session_store.get_account_for_workspace(str(agent_id)) is None
    # The tombstone survives (with its metadata) for future backup access.
    assert session_store.record_store is not None
    records = session_store.record_store.list_records("user-1")
    assert len(records) == 1
    assert records[0].state == "destroyed"


def test_finalize_and_mark_destroying_keeps_failed_when_host_still_up(tmp_path: Path) -> None:
    """A finished destroy whose host is still up is FAILED: kept + stays associated.

    The machine must remain visible and owned so the user can retry, instead
    of vanishing while its host keeps running (and billing).
    """
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    _write_dead_destroy_dir(paths, agent_id, HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(HostId.generate()),
        display_name="kept",
        color=None,
        is_cloud_row=False,
    )
    # Resolver still lists the workspace agent as active -> host still up -> FAILED.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})

    marker = _finalize_and_mark_destroying(paths, backend_resolver, session_store, cli)

    assert marker == {str(agent_id): "failed"}
    assert (paths.data_dir / "destroying" / str(agent_id)).exists()
    assert session_store.get_account_for_workspace(str(agent_id)) is not None


def test_remote_tiles_wait_for_the_initial_discovery_snapshot(tmp_path: Path) -> None:
    """No record renders as a remote tile until discovery has produced its first snapshot.

    Before that, local knowledge is empty and every record -- including this
    device's own machines -- would misclassify as a greyed remote tile.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id="agent-elsewhere",
        host_id="host-elsewhere",
        display_name="remote-ws",
        color=None,
        is_cloud_row=False,
    )

    undiscovered_resolver = MngrCliBackendResolver()
    assert _collect_remote_workspace_tiles(undiscovered_resolver, session_store) == []

    discovered_resolver = make_resolver_with_data(agents_json=make_agents_json(AgentId.generate()))
    tiles = _collect_remote_workspace_tiles(discovered_resolver, session_store)
    assert [tile.agent_id for tile in tiles] == ["agent-elsewhere"]


def _upsert_remote_record(
    session_store: MultiAccountSessionStore,
    *,
    user_id: str,
    email: str,
    agent_id: AgentId,
    provider_kind: str,
    device_label: str,
    encrypted_secrets: str | None,
) -> ReplicaRecord:
    """Store an ACTIVE record for a workspace this device does not host (so it renders as a remote tile)."""
    record_store = session_store.record_store
    assert record_store is not None
    record = ReplicaRecord(
        host_id=str(HostId.generate()),
        agent_id=str(agent_id),
        display_name="elsewhere",
        provider_kind=provider_kind,
        hosting_device_id=None,
        device_label=device_label,
        state=RECORD_STATE_ACTIVE,
        encrypted_secrets=encrypted_secrets,
    )
    record_store.upsert_local_record(user_id, email, record)
    return record


def _encrypt_payload(dek: bytes, payload: WorkspaceSecretsPayload) -> str:
    """The base64 AEAD blob a record carries for ``payload``, as ``decrypt_record_secrets`` expects it."""
    return encode_encrypted_secrets(dek, payload.model_dump_json().encode("utf-8"))


def test_cloud_record_outside_discovery_is_badged_with_its_provider_not_the_creating_device(
    tmp_path: Path,
) -> None:
    """A cloud workspace lives with its provider: the badge must never read as the creating device's hostname.

    The record's ``device_label`` is stamped from whichever device created the
    workspace, so a stopped cloud workspace that falls out of discovery used to
    render as "on <that hostname>" -- nonsense for a workspace in the cloud.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    cloud_agent_id = AgentId.generate()
    _upsert_remote_record(
        session_store,
        user_id="user-1",
        email="a@b.com",
        agent_id=cloud_agent_id,
        provider_kind=imbue_cloud_provider_name_for_account("a@b.com"),
        device_label="mac",
        encrypted_secrets=None,
    )
    other_device_agent_id = AgentId.generate()
    _upsert_remote_record(
        session_store,
        user_id="user-1",
        email="a@b.com",
        agent_id=other_device_agent_id,
        provider_kind="docker",
        device_label="mac",
        encrypted_secrets=None,
    )
    resolver = make_resolver_with_data(agents_json=make_agents_json(AgentId.generate()))

    tile_by_agent_id = {tile.agent_id: tile for tile in _collect_remote_workspace_tiles(resolver, session_store)}

    cloud_tile = tile_by_agent_id[str(cloud_agent_id)]
    assert cloud_tile.kind is RemoteWorkspaceKind.CLOUD
    assert cloud_tile.location == "Imbue Cloud"
    other_device_tile = tile_by_agent_id[str(other_device_agent_id)]
    assert other_device_tile.kind is RemoteWorkspaceKind.OTHER_DEVICE
    assert other_device_tile.location == "mac"


def test_remote_tile_backup_access_follows_where_the_credentials_are(tmp_path: Path) -> None:
    """The tile says whether this device can read the backups now, and if not, why.

    This device's own canonical env always wins (the case of a workspace this
    device provisioned that fell out of discovery); a synced blob it cannot
    decrypt is locked behind the master password; no blob at all means the
    credentials never reached this device. Once the account is unlocked here,
    a synced blob carrying a restic env makes the backups available, while a
    blob with only SSH material (backups never configured) does not.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    record_store = session_store.record_store
    assert record_store is not None
    provider_kind = imbue_cloud_provider_name_for_account("a@b.com")

    local_env_agent_id = AgentId.generate()
    _upsert_remote_record(
        session_store,
        user_id="user-1",
        email="a@b.com",
        agent_id=local_env_agent_id,
        provider_kind=provider_kind,
        device_label="mac",
        encrypted_secrets=None,
    )
    write_canonical_env(record_store.paths, local_env_agent_id, "RESTIC_REPOSITORY=s3:local\nRESTIC_PASSWORD=pw\n")
    locked_agent_id = AgentId.generate()
    _upsert_remote_record(
        session_store,
        user_id="user-1",
        email="a@b.com",
        agent_id=locked_agent_id,
        provider_kind=provider_kind,
        device_label="mac",
        encrypted_secrets="c29tZS1ibG9i",
    )
    secretless_agent_id = AgentId.generate()
    _upsert_remote_record(
        session_store,
        user_id="user-1",
        email="a@b.com",
        agent_id=secretless_agent_id,
        provider_kind=provider_kind,
        device_label="mac",
        encrypted_secrets=None,
    )
    unlocked_user_id = "user-2"
    unlocked_email = "c@d.com"
    cli.add_account(user_id=unlocked_user_id, email=unlocked_email)
    unlocked_provider_kind = imbue_cloud_provider_name_for_account(unlocked_email)
    dek = ensure_dek(record_store.paths, unlocked_user_id)
    synced_agent_id = AgentId.generate()
    _upsert_remote_record(
        session_store,
        user_id=unlocked_user_id,
        email=unlocked_email,
        agent_id=synced_agent_id,
        provider_kind=unlocked_provider_kind,
        device_label="mac",
        encrypted_secrets=_encrypt_payload(
            dek, WorkspaceSecretsPayload(restic_env="RESTIC_REPOSITORY=s3:synced\nRESTIC_PASSWORD=pw\n")
        ),
    )
    ssh_only_agent_id = AgentId.generate()
    _upsert_remote_record(
        session_store,
        user_id=unlocked_user_id,
        email=unlocked_email,
        agent_id=ssh_only_agent_id,
        provider_kind=unlocked_provider_kind,
        device_label="mac",
        encrypted_secrets=_encrypt_payload(dek, WorkspaceSecretsPayload(ssh_private_key="synced-key")),
    )
    resolver = make_resolver_with_data(agents_json=make_agents_json(AgentId.generate()))

    access_by_agent_id = {
        tile.agent_id: tile.backup_access for tile in _collect_remote_workspace_tiles(resolver, session_store)
    }

    assert access_by_agent_id[str(local_env_agent_id)] is BackupAccessState.AVAILABLE
    assert access_by_agent_id[str(locked_agent_id)] is BackupAccessState.LOCKED
    assert access_by_agent_id[str(secretless_agent_id)] is BackupAccessState.UNAVAILABLE
    assert access_by_agent_id[str(synced_agent_id)] is BackupAccessState.AVAILABLE
    assert access_by_agent_id[str(ssh_only_agent_id)] is BackupAccessState.UNAVAILABLE


class _AllAgentsKnownStaticResolver(StaticBackendResolver):
    """Reports every queried agent as a known, host-resolvable agent.

    The inbox display filters out requests whose agent can't be resolved
    to a host (see ``_displayable_pending_requests``). These tests cover
    the running-workspace case where every agent resolves, so the resolver
    claims to know any agent it's asked about.
    """

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="localhost")


def test_build_requests_payload_empty_inbox() -> None:
    """An empty inbox yields a zero count and no pending ids."""
    resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    expected = {"count": 0, "request_ids": []}
    assert _build_requests_payload(None, resolver) == expected
    assert _build_requests_payload(StaticPendingRequests(), resolver) == expected


def test_build_requests_payload_carries_pending_ids() -> None:
    """A pending request surfaces its request_id alongside the count."""
    agent_id = str(AgentId())
    event = create_predefined_permission_request(agent_id=agent_id, scope="slack-api", rationale="post updates")
    resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    payload = _build_requests_payload(StaticPendingRequests(pending=(event,)), resolver)
    assert payload["count"] == 1
    assert payload["request_ids"] == [event.request_id]


def test_build_requests_payload_distinguishes_equal_count_different_contents() -> None:
    """A swap of the pending set at constant size changes the payload.

    This is the soundness property: keying live updates off the bare count
    would miss this transition (count stays 1), so the payload must differ.
    """
    agent_id = str(AgentId())
    request_a = create_predefined_permission_request(agent_id=agent_id, scope="slack-api", rationale="a")
    request_b = create_predefined_permission_request(agent_id=agent_id, scope="github-api", rationale="b")

    inbox_with_a = StaticPendingRequests(pending=(request_a,))
    # Resolve A and add B: the pending set becomes {B}, same size as {A}.
    inbox_with_b = StaticPendingRequests(
        pending=(request_b, request_a),
        answered=(
            create_request_response_event(
                request_event_id=request_a.request_id,
                status=RequestStatus.GRANTED,
                agent_id=agent_id,
            ),
        ),
    )

    resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    payload_a = _build_requests_payload(inbox_with_a, resolver)
    payload_b = _build_requests_payload(inbox_with_b, resolver)
    assert payload_a["count"] == payload_b["count"] == 1
    assert payload_a != payload_b
    assert payload_b["request_ids"] == [request_b.request_id]


# -- Tests for new account management and request routes --


def _create_test_client_with_stores(
    tmp_path: Path,
    cli: ImbueCloudCli | None = None,
    mngr_caller: MngrCaller | None = None,
    # When set, also wired into the app state as ``imbue_cloud_cli`` so routes
    # that reach the connector through ``get_state().imbue_cloud_cli`` (e.g.
    # the accounts plan-view fragment) hit the fake instead of degrading.
    imbue_cloud_cli: ImbueCloudCli | None = None,
    # When set, wired into the app state so routes that reach the backup
    # reaper through ``get_state().sync_scheduler.backup_reaper`` work.
    sync_scheduler: WorkspaceSyncScheduler | None = None,
    # When set, workspace-scoped bug reports actually run the diagnostics
    # collection subprocess -- resolving ``mngr`` via PATH, so tests stub it
    # there -- instead of short-circuiting.
    root_concurrency_group: ConcurrencyGroup | None = None,
) -> tuple[FlaskClient, FileAuthStore]:
    """Create a desktop client with session store and config for testing new routes.

    ``cli`` is forwarded to :func:`make_session_store_for_test` so callers
    can seed the session store with specific accounts; defaults to a
    fresh empty fake CLI. ``mngr_caller`` injects a fake mngr CLI caller (e.g.
    :class:`RecordingMngrCaller`) so routes that shell out (``/help/assist``) can be
    exercised without a real warm process.
    """
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    minds_config = MindsConfig(data_dir=tmp_path)
    request_inbox = StaticPendingRequests()

    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        pending_requests=request_inbox,
        paths=InstallationPaths(data_dir=tmp_path),
        mngr_caller=mngr_caller,
        imbue_cloud_cli=imbue_cloud_cli,
        sync_scheduler=sync_scheduler,
        root_concurrency_group=root_concurrency_group,
    )
    client = app.test_client()
    return client, auth_store


def test_accounts_listing_shows_logged_in_accounts(tmp_path: Path) -> None:
    """The accounts listing the SPA renders carries every logged-in account."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-test-123", email="test@example.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)

    response = client.get("/ui/api/accounts")
    assert response.status_code == 200
    assert "test@example.com" in response.get_data(as_text=True)


def test_account_plan_modal_unknown_account_returns_404(tmp_path: Path) -> None:
    """A user id with no signed-in account is a 404, not a blank modal."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-test-123", email="test@example.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)

    response = client.get("/accounts/user-does-not-exist/plan-modal")

    assert response.status_code == 404


def test_error_reporting_settings_endpoint_persists_toggle(tmp_path: Path) -> None:
    """POST /_chrome/error-reporting persists the single report_unexpected_errors flag live."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)

    assert client.post("/_chrome/error-reporting", json={"report_unexpected_errors": False}).status_code == 200
    assert MindsConfig(data_dir=tmp_path).get_report_unexpected_errors() is False

    assert client.post("/_chrome/error-reporting", json={"report_unexpected_errors": True}).status_code == 200
    assert MindsConfig(data_dir=tmp_path).get_report_unexpected_errors() is True


def test_error_reporting_settings_endpoint_requires_auth(tmp_path: Path) -> None:
    """POST /_chrome/error-reporting rejects an unauthenticated request and records nothing."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/_chrome/error-reporting", json={"report_unexpected_errors": False})
    assert response.status_code == 403
    assert MindsConfig(data_dir=tmp_path).get_report_unexpected_errors() is True


def test_sharing_urls_redirect_to_the_options_panels_share_tab(tmp_path: Path) -> None:
    """Legacy /sharing/<id> URLs land on the Share machine pane, not a 404.

    The standalone sharing editor is gone -- the workspace options panel's
    Share tab is the one sharing surface -- but its URLs were handed out, so
    they redirect. A service segment picks that share target.
    """
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    agent_id = str(AgentId.generate())

    response = client.get(f"/sharing/{agent_id}")
    assert response.status_code == 302
    assert response.headers["Location"] == f"/workspace/{agent_id}/options?tab=share"

    service_response = client.get(f"/sharing/{agent_id}/frontend")
    assert service_response.status_code == 302
    assert service_response.headers["Location"] == f"/workspace/{agent_id}/options?tab=share&target=frontend"

    modal_response = client.get(f"/sharing/{agent_id}/frontend/modal")
    assert modal_response.status_code == 302
    assert modal_response.headers["Location"] == f"/workspace/{agent_id}/options?tab=share&target=frontend"


# -- Workspace options panel routes --


def test_old_requests_panel_route_removed(tmp_path: Path) -> None:
    """The legacy panel route no longer exists."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/_chrome/requests-panel")
    assert response.status_code == 404


def test_old_requests_page_route_removed(tmp_path: Path) -> None:
    """The legacy standalone request page no longer exists."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/requests/evt-anything")
    assert response.status_code == 404


def test_set_default_account(tmp_path: Path) -> None:
    """Setting a default account works correctly."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post(
        "/accounts/set-default",
        data={"user_id": "user-default-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    config = MindsConfig(data_dir=tmp_path)
    assert config.get_default_account_id() == "user-default-123"


# -- welcome-splash skip tests --


def test_welcome_skip_redirects_to_login_when_unauthenticated(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/welcome/skip", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_welcome_skip_sets_flag_and_redirects_home_when_authenticated(tmp_path: Path) -> None:
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/welcome/skip", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert get_state(client.application).is_account_setup_skipped is True


# -- error-reporting consent + settings tests --


def test_consent_page_requires_auth(tmp_path: Path) -> None:
    """GET /consent bounces an unauthenticated request to the login page."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/consent")
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_consent_submit_requires_auth(tmp_path: Path) -> None:
    """POST /consent rejects an unauthenticated request and records nothing."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/consent", json={})
    assert response.status_code == 403
    assert MindsConfig(data_dir=tmp_path).get_error_reporting_consent_given() is False


@pytest.mark.witnesses(
    "home-page.consent-first",
    partial="covers the no-return-destination arrival; the with-return-destination arrival is a separate test",
)
def test_post_login_routes_to_landing_while_consent_unanswered(tmp_path: Path) -> None:
    """While consent is unanswered, post-login routes to "/" (which shows consent), not /accounts."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-test-123", email="test@example.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


@pytest.mark.witnesses("home-page.consent-first")
def test_post_login_overrides_return_to_while_consent_unanswered(tmp_path: Path) -> None:
    """An unanswered consent question overrides every other destination.

    Even with a safe return destination requested and workspaces already
    present -- the two inputs that would otherwise send the user to that path
    or to /accounts -- an arrival at "/post-login" is still redirected to "/"
    (where the consent screen is shown) until the consent question is answered.
    """
    unanswered_config = MindsConfig(data_dir=tmp_path)
    assert unanswered_config.get_error_reporting_consent_given() is False
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {"web": "http://backend"}},
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None, minds_config=unanswered_config
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", query_string={"return_to": "/create"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_backup_password_change_requires_auth(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/_chrome/backup-password", json={"new_password": "x", "new_password_confirm": "x"})
    assert response.status_code == 403


def test_backup_password_change_rejects_mismatched_confirmation(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    response = client.post("/_chrome/backup-password", json={"new_password": "one", "new_password_confirm": "two"})
    assert response.status_code == 400
    assert "match" in response.get_json()["error"]
    assert not bundle_mirror_path(InstallationPaths(data_dir=tmp_path), "user-1").exists()


def test_backup_password_change_requires_a_signed_in_account(tmp_path: Path) -> None:
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post("/_chrome/backup-password", json={"new_password": "x", "new_password_confirm": "x"})
    assert response.status_code == 400
    assert "Sign in" in response.get_json()["error"]


def test_backup_password_change_wraps_the_dek_and_pushes_the_bundle(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    paths = InstallationPaths(data_dir=tmp_path)

    response = client.post(
        "/_chrome/backup-password",
        json={"new_password": "brand-new", "new_password_confirm": "brand-new"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["results"] == [{"account": "a@b.com", "is_ok": True, "error": None}]
    assert verify_master_password_for_account(paths, "user-1", SecretStr("brand-new")) is True
    assert verify_master_password_for_account(paths, "user-1", SecretStr("")) is False
    # The wrapped bundle was pushed to the (fake) connector.
    assert "a@b.com" in cli.sync_bundle_by_email


def test_backup_password_change_may_return_to_the_empty_password(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    paths = InstallationPaths(data_dir=tmp_path)
    assert (
        client.post(
            "/_chrome/backup-password", json={"new_password": "temp", "new_password_confirm": "temp"}
        ).status_code
        == 200
    )

    response = client.post("/_chrome/backup-password", json={"new_password": "", "new_password_confirm": ""})

    assert response.status_code == 200
    assert verify_master_password_for_account(paths, "user-1", SecretStr("")) is True
    # Clearing scrubs the server: no bundle remains on the (fake) connector.
    assert "a@b.com" not in cli.sync_bundle_by_email


def test_backup_password_change_refuses_accounts_locked_on_this_device(tmp_path: Path) -> None:
    """Rewrapping a locked account would mint a fresh DEK and overwrite the
    server bundle wrapping the real one, orphaning every synced secret -- the
    change endpoint must report a failure and touch nothing instead."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    # Another device set a password and synced a secrets-carrying record; this
    # device has no DEK for the account (it is locked here).
    other_device = InstallationPaths(data_dir=tmp_path / "other-device")
    bundle = set_master_password_for_account(other_device, "user-1", SecretStr("hunter2"))
    assert bundle is not None
    cli.sync_bundle_push("a@b.com", bundle)
    remote = ReplicaRecord(
        host_id="host-remote-1",
        agent_id=str(AgentId.generate()),
        display_name="remote-ws",
        provider_kind="lima",
        hosting_device_id="device-other",
        device_label="other-device",
        encrypted_secrets="b3BhcXVl",
    )
    cli.sync_records_by_email["a@b.com"] = {remote.agent_id: remote.to_wire(1)}

    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    session_store = get_state(client.application).session_store
    assert session_store is not None and session_store.record_store is not None
    session_store.record_store.pull("user-1", "a@b.com")
    bundle_before = dict(cli.sync_bundle_by_email["a@b.com"])

    response = client.post(
        "/_chrome/backup-password", json={"new_password": "new-pass", "new_password_confirm": "new-pass"}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is False
    assert body["results"] == [{"account": "a@b.com", "is_ok": False, "error": body["results"][0]["error"]}]
    assert "locked" in body["results"][0]["error"]
    # The server bundle (wrapping the real DEK) is untouched and no divergent
    # local DEK was minted.
    assert cli.sync_bundle_by_email["a@b.com"] == bundle_before
    assert not is_account_unlocked(InstallationPaths(data_dir=tmp_path), "user-1")


# -- get-help / report-a-bug tests --


def test_help_assist_requires_a_workspace(tmp_path: Path) -> None:
    """Agent help is only available inside a machine, so a request without one is rejected."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/assist", json={"description": "it broke"})
    assert response.status_code == 400


def test_help_assist_requires_a_description(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/assist", json={"description": "  ", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 400


def test_help_assist_refuses_a_workspace_without_the_assist_skill(tmp_path: Path) -> None:
    """A machine from an older DEFAULT_WORKSPACE_TEMPLATE (no /assist skill) is refused up front (409) rather than spawning
    a chat that would hang on the unknown ``/assist`` command -- and no ``mngr create`` is attempted."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_ABSENT\n"))
    client, _ = _create_test_client_with_stores(tmp_path, mngr_caller=caller)
    response = client.post("/help/assist", json={"description": "it broke", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 409
    assert "agent-assist skill" in response.get_json()["error"]
    # Only the probe ran; we never attempted to create the chat.
    assert len(caller.calls) == 1
    assert caller.calls[0][0] == "exec"


def test_help_assist_reports_unreachable_workspace(tmp_path: Path) -> None:
    """When the probe can't run (no sentinel -- host down/timeout), we return 502 rather than guess."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="connection refused"))
    client, _ = _create_test_client_with_stores(tmp_path, mngr_caller=caller)
    response = client.post("/help/assist", json={"description": "it broke", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 502
    assert len(caller.calls) == 1


def test_help_assist_spawns_when_the_skill_is_present(tmp_path: Path) -> None:
    """A supported machine probes clean, then the chat is created (probe call + create call)."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_PRESENT\n"))
    client, _ = _create_test_client_with_stores(tmp_path, mngr_caller=caller)
    response = client.post("/help/assist", json={"description": "it broke", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 200
    # First the skill probe, then the inner ``mngr create``.
    assert len(caller.calls) == 2
    assert caller.calls[0][0] == "exec"
    assert caller.calls[1][:2] == ["exec", "--agent"]
    assert "mngr create" in caller.calls[1][3]


def test_help_report_requires_description(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/report", json={"description": "  "})
    assert response.status_code == 400


def test_help_report_accepts_a_description(tmp_path: Path) -> None:
    # Sentry is not initialized in tests, so the report is collected and the route returns ok with a
    # null event_id (nothing was actually transmitted). This exercises the full collect path end to end.
    client, _ = _create_test_client_with_stores(tmp_path)
    # App diagnostics are always collected server-side now; the request need not opt in.
    response = client.post(
        "/help/report",
        json={"description": "the app froze", "remote_access": True},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["event_id"] is None


def _write_console_tail(tmp_path: Path, contents: str) -> Path:
    """Write the rolling console tail the Electron shell keeps, as the route expects to find it."""
    log_dir = InstallationPaths(data_dir=tmp_path).log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    tail_path = log_dir / ELECTRON_CONSOLE_TAIL_FILENAME
    tail_path.write_text(contents)
    return tail_path


def test_help_report_outside_a_workspace_still_attaches_the_captured_console(tmp_path: Path) -> None:
    """The console is the shell's own output, so no workspace is needed to attach it.

    It stages app-side, unscanned by decision -- the same standing as
    ``electron.log`` and ``minds.log``, which already upload unscanned on every
    event. Under the retired design the console could only leave after an
    in-container scan, so a report filed outside a workspace lost it; that
    requirement went with the scan, and this pins the new behavior so nobody
    silently reintroduces the workspace dependency.
    """
    client, _ = _create_test_client_with_stores(tmp_path)
    tail_text = "2026-01-01T00:00:00Z [console:ERROR] boom (app.js:1)\n"
    tail_path = _write_console_tail(tmp_path, tail_text)
    with recording_s3_bucket() as uploads, registered_attachments_uploader(ErrorAttachmentsS3Uploader()):
        with capturing_sentry_client() as captured_events:
            response = client.post("/help/report", json={"description": "the app froze", "include_logs": True})
    assert response.status_code == 200
    report = _submitted_report(captured_events[0])
    # No note: the request flags already say the workspace content was never
    # on offer outside a workspace, and the console attached.
    assert report["collection_note"] is None
    assert report["logs_requested"] is True
    # Staged is not attached: the console must ARRIVE -- an event extra naming its
    # uploaded copy, and the tail's bytes at the uploaded key. Without this, losing
    # the one app.py line that maps the staged file into report_file_paths would
    # leave the console staged on disk forever while every report ships without it.
    extra: Mapping[str, Any] = captured_events[0]["extra"]
    assert f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_console" in extra, sorted(extra)
    # The S3 key is minted from the staged file's own name; the logical
    # bug_report_console name lives only in the event extra asserted above.
    console_uploads = [(key, body) for key, body in uploads if key.startswith("console.log")]
    assert len(console_uploads) == 1, [key for key, _ in uploads]
    assert gzip.decompress(console_uploads[0][1]).decode("utf-8") == tail_text
    # The rolling file is app-lifetime history; a report copies rather than consumes it.
    assert tail_path.exists()


def test_help_report_never_attaches_a_file_for_an_unticked_box(tmp_path: Path) -> None:
    """An unticked box contributes no file to the event.

    Attachments are named one by one, by exact path, so a file this report did
    not ask for cannot reach the event -- and the recorded request flags say
    the transcript was never asked for, whatever the collection that did run
    ends up doing.
    """
    client, _ = _create_test_client_with_stores(tmp_path)

    with capturing_sentry_client() as captured_events:
        response = client.post(
            "/help/report",
            json={
                "description": "it broke",
                "workspace_agent_id": "agent-" + "0" * 32,
                "include_logs": True,
                "include_transcript": False,
            },
        )

    assert response.status_code == 200
    extra: Mapping[str, Any] = captured_events[0]["extra"]
    assert f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_workspace" not in extra
    report = TypeAdapter(dict[str, Any]).validate_python(extra["bug_report"])
    assert report["transcript_requested"] is False


def test_help_report_resolves_immediately_when_collection_cannot_run(
    tmp_path: Path,
) -> None:
    """A report that cannot collect resolves immediately, with the note saying why.

    The minimal test app has no root concurrency group, so a workspace-scoped report can neither run
    the collection exec nor hand it to a background strand. That resolves to a report answered
    immediately: the note says collection could not run, and the request flags record what was asked.
    """
    client, _ = _create_test_client_with_stores(tmp_path)
    with capturing_sentry_client() as captured_events:
        response = client.post(
            "/help/report",
            json={
                "description": "it broke",
                "workspace_agent_id": "agent-" + "0" * 32,
                "include_logs": False,
                "include_transcript": True,
            },
        )
    assert response.status_code == 200
    assert len(captured_events) == 1
    # ``Event`` types its ``extra`` values as ``object``, so the report is validated back into a
    # mapping rather than assumed to be one (same pattern as report_collector_test).
    extra: Mapping[str, Any] = captured_events[0]["extra"]
    report = TypeAdapter(dict[str, Any]).validate_python(extra["bug_report"])
    assert report["collection_note"] == "workspace collection could not run in this app"
    assert report["logs_requested"] is False
    assert report["transcript_requested"] is True


def test_help_report_stages_the_console_even_when_the_collection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The console stages app-side at plan time, untouched by a failing collection.

    The fake mngr -- installed first on PATH, which is how collection resolves
    the real binary -- answers with a failure envelope, so the workspace
    archive never materializes and the status document records the failure
    note. The console still stages and attaches: it is the shell's own output
    and rides no exec, which is exactly what its staging being decoupled from
    the collection buys.
    """
    failure_envelope = exec_json_envelope("", success=False, stderr="agent is not running")
    install_stub_mngr_on_path(tmp_path / "failing-mngr-bin", monkeypatch, f"echo '{failure_envelope}'\nexit 1")
    client, _ = _create_test_client_with_stores(tmp_path, root_concurrency_group=root_concurrency_group)
    tail_text = "2026-01-01T00:00:00Z [console:ERROR] boom (app.js:1)\n"
    _write_console_tail(tmp_path, tail_text)
    with recording_s3_bucket() as uploads, registered_attachments_uploader(ErrorAttachmentsS3Uploader()):
        with capturing_sentry_client() as captured_events:
            response = client.post(
                "/help/report",
                json={
                    "description": "it broke",
                    "workspace_agent_id": "agent-" + "0" * 32,
                    "include_logs": True,
                    "include_transcript": False,
                },
            )
        assert response.status_code == 200
        assert len(captured_events) == 1
        event = captured_events[0]
        report = _submitted_report(event)
        # The console resolved at plan time and attached; only the workspace
        # archive is left to the (doomed) background collection, and the note
        # points a reader at the status document that will say how it went.
        assert report["collection_note"] is not None and "bug_report_attachment_status" in report["collection_note"]
        console_key = _s3_key_from_uri(_uploaded_files_uri(event, "bug_report_console"))
        assert gzip.decompress(_wait_for_upload(uploads, console_key)).decode("utf-8") == tail_text
        # Waited out so the background strand finishes inside the recording
        # bucket rather than uploading into a torn-down one.
        status_key = _s3_key_from_uri(_uploaded_files_uri(event, "bug_report_attachment_status"))
        status_document = _wait_for_upload(uploads, status_key).decode("utf-8")
        assert "workspace archive: not attached (workspace collection failed:" in status_document


def _submitted_report(event: Event) -> dict[str, Any]:
    """The structured bug report as it reached Sentry.

    ``Event`` types its ``extra`` values as ``object``, so the report is validated back into a mapping
    rather than assumed to be one.
    """
    extra: Mapping[str, Any] = event["extra"]
    return TypeAdapter(dict[str, Any]).validate_python(extra["bug_report"])


def _uploaded_files_uri(event: Event, name: str) -> str:
    """The single ``s3://`` uri the event published for one attachment name."""
    extra: Mapping[str, Any] = event["extra"]
    uris = TypeAdapter(list[str]).validate_python(extra[f"{EXTRAS_UPLOADED_FILES_KEY}_{name}"])
    assert len(uris) == 1
    return uris[0]


def _s3_key_from_uri(uri: str) -> str:
    """The bucket-relative key an ``s3://`` uri names, against the test bucket."""
    prefix = f"s3://{TEST_S3_BUCKET}/"
    assert uri.startswith(prefix), uri
    return uri[len(prefix) :]


def _wait_for_upload(uploads: list[tuple[str, bytes]], key: str, timeout_seconds: float = 60.0) -> bytes:
    """Block until the background strand uploads ``key``, returning what it wrote.

    The whole point of the route under test is that it answers before this has
    happened, so a test that wants to read the result has to wait for it.
    """
    contents, _polls, _elapsed = poll_for_value(
        lambda: next((uploaded for uploaded_key, uploaded in list(uploads) if uploaded_key == key), None),
        timeout=timeout_seconds,
        poll_interval=0.05,
    )
    assert contents is not None, f"nothing was uploaded to {key} within {timeout_seconds}s"
    return contents


def _install_blocking_mngr_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started_path: Path, release_path: Path
) -> None:
    """Install an ``mngr`` stub first on PATH that announces it started, waits to be released, then fails.

    Lets a test hold the diagnostics collection open while it inspects what the
    submit already answered, without depending on how long anything takes. Both
    of a collection's execs (the gateway-tail mirror's and the collector's) run this
    same stub; the first to run consumes the wait, and either way both end as
    failed runs, so the collection resolves to a failure note and no archive.
    """
    install_stub_mngr_on_path(
        tmp_path / "blocking-mngr-bin",
        monkeypatch,
        f'echo started > "{started_path}"\n'
        + blocking_release_wait_body(release_path)
        + "\necho 'Error: agent is not running'\nexit 1",
    )


def _wait_for_path(path: Path, timeout_seconds: float = 30.0) -> None:
    """Block until ``path`` exists, so a test can act on work another thread started."""
    wait_for(
        path.exists,
        timeout=timeout_seconds,
        poll_interval=0.05,
        error_message=f"{path} never appeared within {timeout_seconds}s",
    )


def test_help_report_answers_before_its_attachments_have_been_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The submit returns a report id while the collection is demonstrably still running.

    Collection reaches into the workspace and takes tens of seconds, so a report
    with no finished prefetch to reuse *reserves* an S3 location per staged file
    (minting a key is purely local) and publishes those uris on the event now,
    leaving the collection and the uploads to a background strand. The fake mngr
    here blocks until this test releases it, so the response being in hand while
    it is still blocked is proof that nothing was collected on the request thread.
    """
    started_path = tmp_path / "collection-started"
    release_path = tmp_path / "collection-released"
    _install_blocking_mngr_on_path(tmp_path, monkeypatch, started_path, release_path)
    client, _ = _create_test_client_with_stores(tmp_path, root_concurrency_group=root_concurrency_group)
    _write_console_tail(tmp_path, "2026-01-01T00:00:00Z [console:ERROR] boom (app.js:1)\n")
    agent_id = "agent-" + "0" * 32

    with recording_s3_bucket() as uploads, registered_attachments_uploader(ErrorAttachmentsS3Uploader()):
        with capturing_sentry_client() as captured_events:
            response = client.post(
                "/help/report",
                json={
                    "description": "it broke",
                    "workspace_agent_id": agent_id,
                    "include_logs": True,
                    "include_transcript": False,
                },
            )
        assert response.status_code == 200
        event_id = response.get_json()["event_id"]
        assert event_id is not None

        # The collection the report is waiting on has not finished -- it has not
        # even been let past its first step.
        _wait_for_path(started_path)
        assert not release_path.exists()

        event = captured_events[0]
        report = _submitted_report(event)
        # The note is the event's whole story about the archive: collection is
        # still running, and the outcome will be in the status document. The
        # console is not mentioned: it needed no collection, so it resolved
        # (and attached) at plan time.
        assert report["collection_note"] is not None and "bug_report_attachment_status" in report["collection_note"]
        # Uploads are reserved per staged FILE (the workspace archive), not per
        # content type -- the retired per-content names must not come back.
        extra: Mapping[str, Any] = event["extra"]
        assert f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_workspace_logs" not in extra
        assert f"{EXTRAS_UPLOADED_FILES_KEY}_bug_report_transcript" not in extra
        workspace_key = _s3_key_from_uri(_uploaded_files_uri(event, "bug_report_workspace"))
        console_key = _s3_key_from_uri(_uploaded_files_uri(event, "bug_report_console"))
        status_key = _s3_key_from_uri(_uploaded_files_uri(event, "bug_report_attachment_status"))
        # Nothing reserved has been written while the collection is blocked. The
        # console is deliberately not in this list: its one-shot upload rides
        # the event's own callbacks, not the blocked collection.
        assert not [key for key, _contents in uploads if key in (workspace_key, status_key)]

        release_path.write_text("go\n")
        status_document = _wait_for_upload(uploads, status_key).decode("utf-8")

        # The status document is what makes the pending event honest: a reader
        # follows it to learn what the archive actually did.
        assert status_document.startswith(f"bug report event: {event_id}\nworkspace: {agent_id}\n\n")
        assert "workspace archive: not attached (workspace collection failed:" in status_document
        # The console never depended on the collection, so its bytes reach its
        # own uploaded key regardless of the failed execs.
        _wait_for_upload(uploads, console_key)
    # The collection produced no workspace archive, so nothing was written to
    # the uri the event published for it.
    assert not [key for key, _contents in uploads if key == workspace_key]


def test_help_report_reserves_the_workspace_archive_under_a_key_naming_the_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The reserved workspace key ends ``.zip``, and the console's ``.gz``.

    The workspace content stages as one archive that already carries its own
    compression, and the suffix on the reserved key is the only thing the upload
    later reads to decide whether to gzip. Reserving it as ``.gz`` would wrap the
    zip in a gzip, so whoever follows the uri published on the event unwraps two
    layers to reach the conversations they asked for. The key is minted before
    the bytes exist, so nothing but this assertion observes the choice.
    """
    started_path = tmp_path / "collection-started"
    release_path = tmp_path / "collection-released"
    _install_blocking_mngr_on_path(tmp_path, monkeypatch, started_path, release_path)
    client, _ = _create_test_client_with_stores(tmp_path, root_concurrency_group=root_concurrency_group)
    _write_console_tail(tmp_path, "2026-01-01T00:00:00Z [console:ERROR] boom (app.js:1)\n")

    with recording_s3_bucket() as uploads, registered_attachments_uploader(ErrorAttachmentsS3Uploader()):
        with capturing_sentry_client() as captured_events:
            response = client.post(
                "/help/report",
                json={
                    "description": "it broke",
                    "workspace_agent_id": "agent-" + "0" * 32,
                    "include_logs": True,
                    "include_transcript": True,
                },
            )
        assert response.status_code == 200
        event = captured_events[0]
        assert _submitted_report(event)["collection_note"] is not None
        workspace_uri = _uploaded_files_uri(event, "bug_report_workspace")
        assert workspace_uri.endswith(".zip"), workspace_uri
        # The logs are plain text and still gzipped on the way up, so the
        # suffix is chosen per staged file rather than switched wholesale.
        console_uri = _uploaded_files_uri(event, "bug_report_console")
        assert console_uri.endswith(f".{COMPRESSED_LOG_EXTENSION}"), console_uri

        status_key = _s3_key_from_uri(_uploaded_files_uri(event, "bug_report_attachment_status"))
        release_path.write_text("go\n")
        # Waited out so the background strand finishes inside the recording
        # bucket rather than uploading into a torn-down one.
        _wait_for_upload(uploads, status_key)


def _create_test_client_with_api_key(tmp_path: Path, api_key: str) -> FlaskClient:
    """Build a client with the /api/v1 blueprint mounted and a known central API key."""
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    session_store = make_session_store_for_test(tmp_path)
    minds_config = MindsConfig(data_dir=tmp_path)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        paths=InstallationPaths(data_dir=tmp_path),
        minds_api_key=api_key,
    )
    return app.test_client()


def test_api_v1_bug_report_requires_bearer_token(tmp_path: Path) -> None:
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    response = client.post(f"/api/v1/agents/{AgentId()}/report", json={"description": "boom"})
    assert response.status_code == 401


def test_api_v1_bug_report_opens_prefilled_modal_instead_of_submitting(tmp_path: Path) -> None:
    """The agent report route does not submit to Sentry: it asks the app to open the report modal
    pre-filled with the agent's description, scoped to the caller's own machine."""
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    agent_id = AgentId()
    client_queue = get_state(client.application).ui_channel_broadcaster.register()
    response = client.post(
        f"/api/v1/agents/{agent_id}/report",
        json={"description": "agent saw an error"},
        headers={"Authorization": "Bearer secret-key"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    # No Sentry submission happens here, so there is no event_id to return.
    assert "event_id" not in body
    # The route published an open_help frame (scoped to the caller's workspace) instead of submitting.
    assert json.loads(client_queue.get_nowait() or "") == {
        "type": "open_help",
        "description": "agent saw an error",
        "workspace_agent_id": str(agent_id),
    }


def test_api_v1_bug_report_rejects_empty_description(tmp_path: Path) -> None:
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    response = client.post(
        f"/api/v1/agents/{AgentId()}/report",
        json={"description": ""},
        headers={"Authorization": "Bearer secret-key"},
    )
    # An empty description fails the request model's min-length structurally, so
    # it is rejected with the uniform 422 validation contract.
    assert response.status_code == 422
    assert any(error["field"] == "description" for error in response.get_json()["errors"])


# -- system-interface health + recovery tests --


def _await_workspaces_frame(client_queue: "queue.Queue[str | None]", timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Block for the next ``workspaces`` frame on one connection's queue.

    The publish strand wakes on an event, so this returns in well under a
    millisecond in practice; the budget is only there so a wake that never comes
    fails the test with a sentence instead of hanging it, which is why it sits
    well inside the suite's own per-test timeout. Frames of other types (the
    health edge publishes its own) are skipped.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            raw = client_queue.get(timeout=deadline - time.monotonic())
        except queue.Empty:
            break
        if raw is None:
            continue
        frame = json.loads(raw)
        if frame.get("type") == "workspaces":
            return frame
    raise AssertionError("no workspaces frame was published within the timeout")


def _await_health_frame(
    client_queue: "queue.Queue[str | None]", status: AgentHealth, timeout_seconds: float = 3.0
) -> dict[str, Any]:
    """Block for the next ``health`` frame reporting ``status``, skipping other frames.

    Health edges are broadcast directly rather than diffed, so this is how a test
    waits on a transition a background strand makes (the unattended start's)
    instead of guessing at a sleep.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            raw = client_queue.get(timeout=deadline - time.monotonic())
        except queue.Empty:
            break
        if raw is None:
            continue
        frame = json.loads(raw)
        if frame.get("type") == "health" and frame.get("status") == status.value:
            return frame
    raise AssertionError(f"no health frame reporting {status.value} was published within the timeout")


def test_a_health_edge_republishes_the_workspace_lists_backend_verdict(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The band's backend verdict turns on the outage onset, so a health edge must re-derive the list.

    ``is_backend_unreachable`` is withheld once the provider error behind it
    predates the onset -- and the onset is recorded by the health tracker, whose
    edges publish only the health frame. Without a workspace-list wake on that
    edge the band would go on naming the backend from its pre-onset answer until
    some unrelated producer happened to fire, which during an outage of the
    machine's own provider can be a full poll interval away.

    Wired the way the app wires it, unattended recovery included: that dispatch
    rides the same stuck edge and clears the probe-failure run, so a verdict
    gated on the *run* would let the withheld error speak again a beat later --
    and for the rest of the episode, since a new run only starts from HEALTHY.
    """
    workspace_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(
        workspace_agent,
        AgentId.generate(),
        workspace_certified_data={"labels": {"workspace": "my-workspace", "is_primary": "true"}},
    )
    record_provider_discovery_error(resolver, "docker", "Docker Desktop is manually paused.")
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        system_interface_health_tracker=tracker,
        root_concurrency_group=root_concurrency_group,
        # Fails the recovery's ``mngr start`` outright, so the dispatch reaches a
        # terminal state within the test instead of parking on the cold-boot
        # readiness wait. RECOVERY_FAILED clears the failure run just as
        # RECOVERING does, so it exercises the same thing.
        mngr_binary=write_stub_mngr(tmp_path, "mngr", "exit 1"),
    )
    publisher = get_state(app).ui_publisher
    assert publisher is not None
    client_queue = publisher.broadcaster.register()

    # With no outage of the machine's own yet, the errored poll is the freshest
    # thing said about that backend, so the band may name it. Publishing here
    # also records the frame the next pass diffs against.
    publisher.publish_now()
    published = [frame for frame in drain_ui_channel_frames(client_queue) if frame["type"] == "workspaces"]
    assert published[-1]["workspaces"][0]["is_backend_unreachable"] is True

    publisher.start(root_concurrency_group)
    try:
        # The machine now stops answering for reasons of its own, which records an
        # onset the errored poll predates. This edge is the only producer that fires.
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)

        frame = _await_workspaces_frame(client_queue)
        assert frame["workspaces"][0]["is_backend_unreachable"] is False

        # The unattended start has by now run and cleared the failure run. The
        # backend is still not something this episode has observed, so it still
        # may not be named.
        _await_health_frame(client_queue, AgentHealth.RECOVERY_FAILED)
        publisher.publish_now()
        after_restart = [frame for frame in drain_ui_channel_frames(client_queue) if frame["type"] == "workspaces"]
        latest = after_restart[-1] if after_restart else frame
        assert latest["workspaces"][0]["is_backend_unreachable"] is False
    finally:
        # The strand parks on its wake event, so the group's exit would time out
        # waiting for it -- and that failure would mask a failing assertion above.
        publisher.stop()


def test_create_desktop_client_stashes_system_interface_health_tracker(tmp_path: Path) -> None:
    """create_desktop_client should expose the tracker on the app state for handlers."""
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    tracker = SystemInterfaceHealthTracker()
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        system_interface_health_tracker=tracker,
    )

    assert get_state(app).system_interface_health_tracker is tracker


# -- sync unlock / remove-record tests --


def test_sync_unlock_installs_the_dek_for_a_locked_account(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    # Another device set a password and synced a workspace with secrets: the
    # bundle + a secret-carrying record exist on the (fake) connector, but
    # this device has no DEK file.
    other_device = InstallationPaths(data_dir=tmp_path / "other-device")
    bundle = set_master_password_for_account(other_device, "user-1", SecretStr("hunter2"))
    assert bundle is not None
    cli.sync_bundle_push("a@b.com", bundle)
    remote = ReplicaRecord(
        host_id="host-remote-1",
        agent_id=str(AgentId.generate()),
        display_name="remote-ws",
        provider_kind="lima",
        hosting_device_id="device-other",
        device_label="other-device",
        encrypted_secrets="b3BhcXVl",
    )
    cli.sync_records_by_email["a@b.com"] = {remote.agent_id: remote.to_wire(1)}

    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    # The reconcile normally pulls on startup; do it directly for the test.
    session_store = get_state(client.application).session_store
    assert session_store is not None and session_store.record_store is not None
    session_store.record_store.pull("user-1", "a@b.com")

    wrong = client.post("/_chrome/sync-unlock", json={"password": "nope"})
    assert wrong.status_code == 200
    assert wrong.get_json()["ok"] is False

    response = client.post("/_chrome/sync-unlock", json={"password": "hunter2"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["unlocked"] == ["a@b.com"]
    assert is_account_unlocked(InstallationPaths(data_dir=tmp_path), "user-1")


def test_sync_unlock_requires_auth(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    assert client.post("/_chrome/sync-unlock", json={"password": "x"}).status_code == 403


def test_remove_workspace_record_deletes_the_row(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    session_store = get_state(client.application).session_store
    assert session_store is not None
    workspace_id = str(AgentId.generate())
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=workspace_id,
        host_id="host-remove-me",
        display_name="stale",
        color=None,
        is_cloud_row=False,
    )
    assert workspace_id in cli.sync_records_by_email["a@b.com"]

    # Removal still addresses the row by its legacy host id; the fake resolves
    # it through the row's host column, like the connector's compat shim.
    response = client.post("/_chrome/workspaces/remove-record", json={"host_id": "host-remove-me"})

    assert response.status_code == 200
    assert workspace_id not in cli.sync_records_by_email["a@b.com"]
    assert session_store.record_store is not None
    assert session_store.record_store.list_records("user-1") == []


def test_remove_workspace_record_of_a_leased_cloud_workspace_is_refused_with_a_hint(tmp_path: Path) -> None:
    """The connector's tombstone-first refusal surfaces as a 409 that points at destroy, not a sync error."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    session_store = get_state(client.application).session_store
    assert session_store is not None
    workspace_id = str(AgentId.generate())
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=workspace_id,
        host_id="host-still-leased",
        display_name="live",
        color=None,
        is_cloud_row=True,
    )
    cli.lease_holding_workspace_ids.add(workspace_id)

    response = client.post("/_chrome/workspaces/remove-record", json={"workspace_id": workspace_id})

    assert response.status_code == 409
    assert "destroy the workspace" in response.get_json()["error"]
    assert workspace_id in cli.sync_records_by_email["a@b.com"]


def test_remove_workspace_record_unknown_host_is_404(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    assert client.post("/_chrome/workspaces/remove-record", json={"host_id": "host-nope"}).status_code == 404


def test_finalize_and_mark_destroying_deletes_the_machines_share(tmp_path: Path) -> None:
    """Destroying a machine tears down its machine share.

    Nothing downstream of ``mngr destroy`` knows the share exists, so without
    this the share outlives every identifier that could find it: it keeps a
    relay hostname reserved and counts against a quota measured in machines
    ever created rather than live ones.
    """
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    _write_dead_destroy_dir(paths, agent_id, host_id)
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    cli.add_share(account="a@b.com", host_id=str(host_id))
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(host_id),
        display_name="doomed",
        color=None,
        is_cloud_row=False,
    )
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    _finalize_and_mark_destroying(paths, backend_resolver, session_store, cli)

    assert cli.deleted_share_host_ids == [str(host_id)]
    assert cli.get_share_status(account="a@b.com", host_id=str(host_id)) is None


def test_finalize_and_mark_destroying_tombstones_even_if_the_share_delete_fails(tmp_path: Path) -> None:
    """A connector hiccup must not leave the machine stuck in the UI.

    A share that survives is litter; a machine that cannot be retired is a
    stuck row the user cannot clear.
    """
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    _write_dead_destroy_dir(paths, agent_id, host_id)
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    # The share lookup itself blows up; teardown must still proceed.
    cli.is_share_lookup_failing = True
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(host_id),
        display_name="doomed",
        color=None,
        is_cloud_row=False,
    )
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    marker = _finalize_and_mark_destroying(paths, backend_resolver, session_store, cli)

    assert marker == {}
    assert not (paths.data_dir / "destroying" / str(agent_id)).exists()
    assert session_store.record_store is not None
    assert session_store.record_store.list_records("user-1")[0].state == "destroyed"


def test_forward_bridge_redirects_authenticated_browser_to_plugin(tmp_path: Path) -> None:
    """/forward-bridge bounces a signed-in browser to the plugin's /_bridge with the spawn secret.

    This is browser mode's twin of the Electron preauth cookie injection: the
    chrome iframe enters workspaces through this hop so the plugin can set its
    bare-origin session cookie without an OTP.
    """
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        mngr_forward_port=9876,
        mngr_forward_browser_bridge_token="bridge-tok",
    )
    client = app.test_client()
    _authenticate_client(client, auth_store)
    next_path = "/goto/host-00000000000000000000000000000000/"
    response = client.get(f"/forward-bridge?next={next_path}")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("https://localhost:9876/_bridge?token=bridge-tok&next=")
    assert "goto" in location
    # Off-origin next targets collapse to "/" (no open redirect).
    evil = client.get("/forward-bridge?next=//evil.com/")
    assert evil.headers["Location"].endswith("&next=%2F")


def test_forward_bridge_unauthenticated_redirects_home(tmp_path: Path) -> None:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        mngr_forward_port=9876,
        mngr_forward_browser_bridge_token="bridge-tok",
    )
    client = app.test_client()
    response = client.get("/forward-bridge?next=/goto/host-00000000000000000000000000000000/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_forward_bridge_is_404_without_spawn_token(tmp_path: Path) -> None:
    client, auth_store, _agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client, auth_store)
    assert client.get("/forward-bridge?next=/").status_code == 404
