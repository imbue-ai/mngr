import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg2
import pytest
from fastapi import HTTPException
from ovh.exceptions import ResourceNotFoundError as OvhResourceNotFoundError
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

from imbue.remote_service_connector.app import AdminAuth
from imbue.remote_service_connector.app import AuthPolicy
from imbue.remote_service_connector.app import CloudflareApiError
from imbue.remote_service_connector.app import HttpCloudflareOps
from imbue.remote_service_connector.app import HttpOvhOps
from imbue.remote_service_connector.app import InvalidR2BucketNameError
from imbue.remote_service_connector.app import InvalidTunnelComponentError
from imbue.remote_service_connector.app import R2BucketOwnershipError
from imbue.remote_service_connector.app import ServiceNotFoundError
from imbue.remote_service_connector.app import TunnelComponentTooLongError
from imbue.remote_service_connector.app import TunnelNotFoundError
from imbue.remote_service_connector.app import TunnelOwnershipError
from imbue.remote_service_connector.app import _authenticate_supertokens
from imbue.remote_service_connector.app import _default_email_getter
from imbue.remote_service_connector.app import cf_check
from imbue.remote_service_connector.app import cf_list_all_pages
from imbue.remote_service_connector.app import clean_up_pool_host_in_ovh
from imbue.remote_service_connector.app import clear_paid_status_cache
from imbue.remote_service_connector.app import derive_s3_secret_access_key
from imbue.remote_service_connector.app import extract_service_name
from imbue.remote_service_connector.app import extract_username_from_tunnel_name
from imbue.remote_service_connector.app import is_email_paid
from imbue.remote_service_connector.app import is_email_paid_in_db
from imbue.remote_service_connector.app import make_bucket_name
from imbue.remote_service_connector.app import make_hostname
from imbue.remote_service_connector.app import make_tunnel_name
from imbue.remote_service_connector.app import require_paid_account
from imbue.remote_service_connector.app import run_pool_host_cleanup_sweep
from imbue.remote_service_connector.app import slugify_r2_name
from imbue.remote_service_connector.app import verify_bucket_ownership
from imbue.remote_service_connector.app import vps_urn_for
from imbue.remote_service_connector.testing import FakeOvhOps
from imbue.remote_service_connector.testing import make_fake_forwarding_ctx
from imbue.remote_service_connector.testing import make_fake_pool_backend


def test_make_tunnel_name_format() -> None:
    assert make_tunnel_name("alice", "agent1") == "alice--agent1"


def test_make_tunnel_name_allows_single_hyphen_in_agent_id() -> None:
    assert make_tunnel_name("alice", "agent-abc123") == "alice--abc123"


def test_make_tunnel_name_rejects_double_hyphen_in_username() -> None:
    with pytest.raises(InvalidTunnelComponentError, match="Username"):
        make_tunnel_name("alice--bob", "agent1")


def test_make_tunnel_name_strips_agent_prefix() -> None:
    # "agent--1" only exercises the "agent-" prefix strip (-> "-1"); it is shorter than the
    # 16-char cap, so truncation does not fire here (see the dedicated test below).
    result = make_tunnel_name("alice", "agent--1")
    assert result == "alice---1"


def test_make_tunnel_name_truncates_agent_id_to_sixteen_chars() -> None:
    # After stripping "agent-", a 40-char id must be truncated to exactly 16 chars.
    result = make_tunnel_name("alice", "agent-" + "b" * 40)
    assert result == "alice--" + "b" * 16


def test_make_hostname_format() -> None:
    assert make_hostname("web", "agent1", "alice", "example.com") == "web--agent1--alice.example.com"


def test_extract_service_name_from_hostname() -> None:
    assert extract_service_name("web--agent1--alice.example.com", "agent1", "alice", "example.com") == "web"


def test_extract_service_name_returns_none_for_non_matching() -> None:
    assert extract_service_name("other.example.com", "agent1", "alice", "example.com") is None


def test_extract_username_from_tunnel_name() -> None:
    assert extract_username_from_tunnel_name("alice--agent1") == "alice"


def test_cf_check_raises_on_error() -> None:
    response = httpx.Response(400, json={"success": False, "errors": [{"message": "bad"}]})
    with pytest.raises(CloudflareApiError) as exc_info:
        cf_check(response)
    assert exc_info.value.status_code == 400


def test_cf_check_returns_data_on_success() -> None:
    response = httpx.Response(200, json={"success": True, "result": {"id": "123"}})
    data = cf_check(response)
    assert data["result"]["id"] == "123"


def test_cf_list_all_pages_paginates() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        page = int(dict(request.url.params).get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"id": "1"}, {"id": "2"}],
                    "result_info": {"total_count": 3, "page": 1, "per_page": 2, "count": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [{"id": "3"}],
                "result_info": {"total_count": 3, "page": 2, "per_page": 2, "count": 1},
            },
        )

    client = httpx.Client(base_url="https://test.example.com", transport=httpx.MockTransport(handler))
    results = cf_list_all_pages(client, "/test", {})
    assert len(results) == 3
    assert call_count == 2


def test_create_tunnel() -> None:
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    assert info.tunnel_name == "alice--agent1"
    assert info.token == "token-for-tunnel-1"
    assert info.services == []


def test_create_tunnel_with_default_auth() -> None:
    ctx = make_fake_forwarding_ctx()
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    info = ctx.create_tunnel("alice", "agent1", default_auth_policy=policy)
    assert info.tunnel_name == "alice--agent1"
    stored = ctx.get_tunnel_auth("alice--agent1")
    assert stored is not None
    assert len(stored.rules) == 1


def test_create_tunnel_reuses_existing() -> None:
    ctx = make_fake_forwarding_ctx()
    info1 = ctx.create_tunnel("alice", "agent1")
    info2 = ctx.create_tunnel("alice", "agent1")
    assert info1.tunnel_id == info2.tunnel_id


def test_list_tunnels_filters_by_user() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.create_tunnel("alice", "agent2")
    ctx.create_tunnel("bob", "agent3")
    tunnels = ctx.list_tunnels("alice")
    assert len(tunnels) == 2


def test_delete_tunnel_cascades() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.delete_tunnel("alice--agent1", "alice")
    assert len(ctx.fake.tunnels) == 0
    assert len(ctx.fake.dns_records) == 0
    assert ctx.fake.kv_get("alice--agent1") is None


def test_delete_tunnel_raises_for_wrong_owner() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(TunnelOwnershipError):
        ctx.delete_tunnel("alice--agent1", "bob")


def test_add_service_creates_dns_and_ingress() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    info = ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert info.hostname == "web--agent1--alice.example.com"
    assert len(ctx.fake.dns_records) == 1


def test_add_service_applies_default_access_policy() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert len(ctx.fake.access_apps) == 1
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert len(ctx.fake.access_policies.get(app_id, [])) == 1


def test_add_service_passes_allowed_idps_to_access_app() -> None:
    """When ForwardingCtx has allowed_idps configured, they are passed to created Access Applications."""
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp-uuid-123"])
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert ctx.fake.access_apps[app_id]["allowed_idps"] == ["google-idp-uuid-123"]


def test_add_service_no_allowed_idps_when_not_configured() -> None:
    """When allowed_idps is None, it is not included in the Access Application."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert "allowed_idps" not in ctx.fake.access_apps[app_id]


def test_set_service_auth_passes_allowed_idps() -> None:
    """set_service_auth creates Access Applications with allowed_idps when configured."""
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp-uuid-123", "otp-idp-uuid-456"])
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", policy)
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert ctx.fake.access_apps[app_id]["allowed_idps"] == ["google-idp-uuid-123", "otp-idp-uuid-456"]


def test_add_service_is_idempotent() -> None:
    """Calling ``add_service`` twice for the same hostname should succeed without
    creating a duplicate CNAME or duplicate ingress rule.

    Real Cloudflare returns error 81053 ("DNS record already exists") on the
    second ``create_cname`` call -- ``FakeCloudflareOps`` mirrors that. Before
    this fix, the minds "Update sharing" flow re-ran ``add_service`` on every
    submit and surfaced the connector's 400/81053 error to the user.
    """
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp"])
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:9090")
    assert len(ctx.fake.dns_records) == 1
    services = ctx.list_services("alice--agent1", "alice")
    assert len(services) == 1
    assert services[0].service_url == "http://localhost:9090"


def test_add_service_preserves_customized_service_auth_on_re_add() -> None:
    """A second ``add_service`` after the user has set a custom service-level
    auth policy must not reset that policy back to the tunnel default."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    default_policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "owner@x.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", default_policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")

    custom_policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "guest@y.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", custom_policy)

    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    result = ctx.get_service_auth("alice--agent1", "alice", "web")
    assert result is not None
    assert result.rules == custom_policy.rules


def test_add_service_rejects_cname_pointing_elsewhere() -> None:
    """If a CNAME for the hostname exists but points at a different tunnel,
    ``add_service`` must refuse rather than silently leak traffic."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    hostname = make_hostname("web", "agent1", "alice", "example.com")
    ctx.fake.dns_records.append(
        {"id": "stray", "name": hostname, "content": "different-tunnel.cfargotunnel.com", "type": "CNAME"}
    )
    with pytest.raises(CloudflareApiError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")


def test_remove_service_deletes_access_app() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert len(ctx.fake.access_apps) == 1
    ctx.remove_service("alice--agent1", "alice", "web")
    assert len(ctx.fake.access_apps) == 0


def test_remove_service_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(ServiceNotFoundError):
        ctx.remove_service("alice--agent1", "alice", "nonexistent")


def test_tunnel_auth_get_set() -> None:
    ctx = make_fake_forwarding_ctx()
    assert ctx.get_tunnel_auth("alice--agent1") is None
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    result = ctx.get_tunnel_auth("alice--agent1")
    assert result is not None
    assert result.rules == policy.rules


def test_service_auth_get_set() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", policy)
    result = ctx.get_service_auth("alice--agent1", "alice", "web")
    assert result is not None
    assert len(result.rules) == 1


def test_resolve_tunnel_name_by_id() -> None:
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    name = ctx.resolve_tunnel_name_by_id(info.tunnel_id)
    assert name == "alice--agent1"


def test_resolve_tunnel_name_by_id_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    with pytest.raises(TunnelNotFoundError):
        ctx.resolve_tunnel_name_by_id("nonexistent")


def test_tunnel_component_too_long_error_message() -> None:
    with pytest.raises(TunnelComponentTooLongError) as exc_info:
        raise TunnelComponentTooLongError("Username", "toolong", 5)
    assert "Username" in str(exc_info.value)
    assert "toolong" in str(exc_info.value)
    assert "5" in str(exc_info.value)


# -- _authenticate_supertokens tests --


class _FakeSession:
    """Minimal mock for supertokens SessionContainer."""

    def __init__(self, user_id: str, email_verified: bool = True) -> None:
        self._user_id = user_id
        self._email_verified = email_verified

    def get_user_id(self) -> str:
        return self._user_id

    def get_access_token_payload(self) -> dict[str, object]:
        return {"st-ev": {"v": self._email_verified, "t": 0}}


def test_authenticate_supertokens_returns_admin_auth_with_user_id_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid token returns AdminAuth whose username is the first 16 hex chars of the user ID."""
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    result = _authenticate_supertokens(
        "valid-token",
        session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=True),
        email_getter=lambda _user_id: "alice@example.com",
    )
    assert isinstance(result, AdminAuth)
    assert result.username == "a1b2c3d4e5f67890"
    assert result.email == "alice@example.com"


def test_authenticate_supertokens_returns_admin_auth_with_none_email_when_lookup_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful auth with no resolvable email leaves ``AdminAuth.email`` as None."""
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    result = _authenticate_supertokens(
        "valid-token",
        session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=True),
        email_getter=lambda _user_id: None,
    )
    assert isinstance(result, AdminAuth)
    assert result.email is None


def test_authenticate_supertokens_raises_401_when_email_not_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the email is not verified, raises 401."""
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "valid-token",
            session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=False),
            email_getter=lambda _user_id: "alice@example.com",
        )
    assert exc_info.value.status_code == 401
    assert "verified" in exc_info.value.detail


def test_authenticate_supertokens_raises_401_when_email_verification_claim_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the email verification claim is absent from the payload, raises 401."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")

    class _SessionNoClaim:
        def get_user_id(self) -> str:
            return "a1b2c3d4-e5f6-7890-abcd-1234567890ab"

        def get_access_token_payload(self) -> dict[str, object]:
            return {}

    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "valid-token",
            session_getter=lambda **kwargs: _SessionNoClaim(),
            email_getter=lambda _user_id: None,
        )
    assert exc_info.value.status_code == 401
    assert "verified" in exc_info.value.detail


def test_authenticate_supertokens_raises_401_when_connection_uri_not_set() -> None:
    """When SUPERTOKENS_CONNECTION_URI is absent, raises 401."""
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "any-token",
            session_getter=lambda **kwargs: _FakeSession("ignored"),
            email_getter=lambda _user_id: None,
        )
    assert exc_info.value.status_code == 401
    assert "not configured" in exc_info.value.detail


def test_authenticate_supertokens_raises_401_when_session_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the session getter returns None, raises 401."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "expired-token",
            session_getter=lambda **kwargs: None,
        )
    assert exc_info.value.status_code == 401


def test_authenticate_supertokens_raises_401_on_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the session getter raises SuperTokensSessionError, raises 401."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")

    def _raise(**kwargs: object) -> None:
        raise SuperTokensSessionError("bad session")

    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens("bad-token", session_getter=_raise)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


def test_authenticate_supertokens_raises_401_on_general_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK is not initialized (GeneralError), raises 401 instead of 500."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")

    def _raise(**kwargs: object) -> None:
        raise SuperTokensGeneralError("Initialisation not done")

    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens("bad-token", session_getter=_raise)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


# -- _default_email_getter tests --


class _FakeLoginMethod:
    """Stand-in for a SuperTokens LoginMethod -- only ``email`` and ``verified`` are used."""

    def __init__(self, email: str | None, verified: bool = True) -> None:
        self.email = email
        self.verified = verified


class _FakeStUser:
    """Stand-in for a SuperTokens User -- only the ``login_methods`` attribute is used."""

    def __init__(self, login_methods: list[_FakeLoginMethod]) -> None:
        self.login_methods = login_methods


def test_default_email_getter_returns_first_verified_non_empty_email() -> None:
    """The first login method with both a non-empty email and ``verified=True`` is returned."""
    user = _FakeStUser([_FakeLoginMethod(None), _FakeLoginMethod(""), _FakeLoginMethod("alice@example.com")])
    assert _default_email_getter("user-123", user_getter=lambda _user_id: user) == "alice@example.com"


def test_default_email_getter_skips_unverified_emails() -> None:
    """Unverified login methods are skipped; the first *verified* email is returned.

    A user with both an unverified third-party login (``evil@gmail.com``) and a verified
    emailpassword login (``alice@imbue.com``) must surface ``alice@imbue.com``, since the
    paid-feature gate authorizes by domain ownership and only verified emails prove that.
    """
    user = _FakeStUser(
        [
            _FakeLoginMethod("evil@gmail.com", verified=False),
            _FakeLoginMethod("alice@imbue.com", verified=True),
        ]
    )
    assert _default_email_getter("user-123", user_getter=lambda _user_id: user) == "alice@imbue.com"


def test_default_email_getter_returns_none_when_only_unverified_emails() -> None:
    """When every login method is unverified, returns None even if emails are present."""
    user = _FakeStUser(
        [
            _FakeLoginMethod("evil@gmail.com", verified=False),
            _FakeLoginMethod("other@gmail.com", verified=False),
        ]
    )
    assert _default_email_getter("user-123", user_getter=lambda _user_id: user) is None


def test_default_email_getter_returns_none_when_no_login_method_has_email() -> None:
    """When no login method has a non-empty email, returns None."""
    user = _FakeStUser([_FakeLoginMethod(None), _FakeLoginMethod("")])
    assert _default_email_getter("user-123", user_getter=lambda _user_id: user) is None


def test_default_email_getter_returns_none_when_user_is_none() -> None:
    """When the SDK reports no user for the id, returns None."""
    assert _default_email_getter("user-123", user_getter=lambda _user_id: None) is None


def test_default_email_getter_returns_none_on_general_error() -> None:
    """When the SDK raises a GeneralError (e.g. transient core problem), it is swallowed and None is returned."""

    def _raise(_user_id: str) -> None:
        raise SuperTokensGeneralError("transient core problem")

    assert _default_email_getter("user-123", user_getter=_raise) is None


def test_default_email_getter_returns_none_on_session_error() -> None:
    """When the SDK raises a SessionError, it is swallowed and None is returned."""

    def _raise(_user_id: str) -> None:
        raise SuperTokensSessionError("bad session")

    assert _default_email_getter("user-123", user_getter=_raise) is None


# -- HttpCloudflareOps tests (via httpx.MockTransport) --
#
# HttpCloudflareOps is the production implementation backed by real Cloudflare
# HTTP calls. These tests wire it up with httpx.MockTransport so every cf_*
# helper and its HttpCloudflareOps wrapper runs without touching the network.


def _cf_result(result: object, *, total_count: int | None = None) -> dict[str, object]:
    body: dict[str, object] = {"success": True, "result": result}
    if total_count is not None and isinstance(result, list):
        body["result_info"] = {
            "total_count": total_count,
            "page": 1,
            "per_page": len(result) or 1,
            "count": len(result),
        }
    return body


def _build_http_ops_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpCloudflareOps:
    """Construct an HttpCloudflareOps whose client is wired to a MockTransport.

    Closes the real httpx.Client that HttpCloudflareOps opens during __init__
    before reassigning ``ops.client`` to the mock-backed client, so tests
    don't leak a connection pool per invocation.
    """
    ops = HttpCloudflareOps(api_token="token", account_id="acc", zone_id="zone")
    ops.client.close()
    ops.client = httpx.Client(base_url="https://api.cloudflare.com/client/v4", transport=httpx.MockTransport(handler))
    return ops


def _build_http_ops_with_routes(
    routes: dict[tuple[str, str], httpx.Response],
) -> HttpCloudflareOps:
    """Construct an HttpCloudflareOps whose client is wired to a MockTransport.

    Each key in ``routes`` is ``(method, path_prefix)``; the first matching
    route returns its response. Requests that don't match any route produce a
    clear AssertionError instead of a silent 404 so new uncovered code paths
    fail loudly in test output.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        for (method, path), response in routes.items():
            if request.method == method and request.url.path.startswith(path):
                return response
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    return _build_http_ops_with_handler(handler)


def test_http_ops_tunnel_roundtrip() -> None:
    """create_tunnel, list_tunnels, get_tunnel_by_name/id, get_tunnel_token, delete_tunnel."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("POST", "/client/v4/accounts/acc/cfd_tunnel"): httpx.Response(
            200, json=_cf_result({"id": "t1", "name": "alice--a1"})
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/t1/token"): httpx.Response(
            200, json=_cf_result("tunnel-token-value")
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/t1"): httpx.Response(
            200, json=_cf_result({"id": "t1", "name": "alice--a1"})
        ),
        ("GET", "/client/v4/accounts/acc/cfd_tunnel"): httpx.Response(
            200, json=_cf_result([{"id": "t1", "name": "alice--a1"}], total_count=1)
        ),
        ("DELETE", "/client/v4/accounts/acc/cfd_tunnel/t1"): httpx.Response(200, json=_cf_result(None)),
    }
    ops = _build_http_ops_with_routes(routes)
    tunnel = ops.create_tunnel("alice--a1")
    assert tunnel["id"] == "t1"
    assert ops.get_tunnel_token("t1") == "tunnel-token-value"
    assert ops.get_tunnel_by_id("t1") == {"id": "t1", "name": "alice--a1"}
    by_name = ops.get_tunnel_by_name("alice--a1")
    assert by_name is not None and by_name["id"] == "t1"
    tunnels = ops.list_tunnels(include_prefix="alice")
    assert len(tunnels) == 1
    ops.delete_tunnel("t1")


def test_http_ops_get_tunnel_by_id_returns_none_on_404() -> None:
    """cf_get_tunnel_by_id returns None (not raising) when the tunnel is missing."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("GET", "/client/v4/accounts/acc/cfd_tunnel/missing"): httpx.Response(
            404, json={"success": False, "errors": [{"message": "not found"}]}
        ),
    }
    ops = _build_http_ops_with_routes(routes)
    assert ops.get_tunnel_by_id("missing") is None


def test_http_ops_tunnel_config_roundtrip() -> None:
    """get_tunnel_config and put_tunnel_config both route through cf_check."""
    put_calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/configurations" in request.url.path:
            return httpx.Response(200, json=_cf_result({"config": {"ingress": []}}))
        if request.method == "PUT" and "/configurations" in request.url.path:
            put_calls.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    ops = _build_http_ops_with_handler(handler)
    config = ops.get_tunnel_config("t1")
    assert "config" in config
    ops.put_tunnel_config("t1", {"config": {"ingress": [{"service": "http_status:404"}]}})
    assert len(put_calls) == 1


def test_http_ops_dns_record_roundtrip() -> None:
    """create_cname, list_dns_records (with filter), delete_dns_record."""
    created: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/dns_records"):
            created.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result({"id": "r1", "name": "x.example.com"}))
        if request.method == "GET" and request.url.path.endswith("/dns_records"):
            return httpx.Response(
                200,
                json=_cf_result([{"id": "r1", "name": "x.example.com"}], total_count=1),
            )
        if request.method == "DELETE" and "/dns_records/r1" in request.url.path:
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    ops = _build_http_ops_with_handler(handler)
    record = ops.create_cname("x.example.com", "target.example.com")
    assert record["id"] == "r1"
    assert created[0]["type"] == "CNAME"
    assert created[0]["proxied"] is True
    records = ops.list_dns_records(name="x.example.com")
    assert len(records) == 1
    ops.delete_dns_record("r1")


def test_http_ops_access_app_and_policies_roundtrip() -> None:
    """Full Access Application + policy lifecycle flows through the real wrappers."""
    policies: list[dict[str, object]] = []
    created_apps: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/access/apps"):
            created_apps.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=_cf_result({"id": "app1", "domain": "x.example.com"}))
        if request.method == "GET" and path.endswith("/access/apps"):
            return httpx.Response(200, json=_cf_result([{"id": "app1", "domain": "x.example.com"}]))
        if request.method == "DELETE" and "/access/apps/app1/policies/p1" in path:
            return httpx.Response(200, json=_cf_result(None))
        if request.method == "DELETE" and path.endswith("/access/apps/app1"):
            return httpx.Response(200, json=_cf_result(None))
        if request.method == "GET" and "/access/apps/app1/policies" in path:
            return httpx.Response(200, json=_cf_result(list(policies)))
        if request.method == "POST" and "/access/apps/app1/policies" in path:
            body = json.loads(request.content.decode())
            policy_record = {**body, "id": "p1"}
            policies.append(policy_record)
            return httpx.Response(200, json=_cf_result(policy_record))
        if request.method == "PUT" and "/access/apps/app1/policies/p1" in path:
            body = json.loads(request.content.decode())
            policies[0] = {**body, "id": "p1"}
            return httpx.Response(200, json=_cf_result(policies[0]))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    ops.create_access_app("x.example.com", "My App", allowed_idps=["idp-1"])
    assert created_apps[0]["allowed_idps"] == ["idp-1"]
    by_domain = ops.get_access_app_by_domain("x.example.com")
    assert by_domain is not None and by_domain["id"] == "app1"
    created_policy = ops.create_access_policy("app1", {"name": "allow", "decision": "allow"})
    assert created_policy["id"] == "p1"
    listed = ops.list_access_policies("app1")
    assert len(listed) == 1
    ops.update_access_policy("app1", "p1", {"name": "allow-updated", "decision": "allow"})
    assert ops.list_access_policies("app1")[0]["name"] == "allow-updated"
    ops.delete_access_policy("app1", "p1")
    ops.delete_access_app("app1")


def test_http_ops_kv_namespace_create_when_missing() -> None:
    """kv_get/kv_put/kv_delete + namespace creation path."""
    stored: dict[str, str] = {}
    namespace_create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal namespace_create_count
        path = request.url.path
        if request.method == "GET" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(200, json=_cf_result([]))
        if request.method == "POST" and path.endswith("/storage/kv/namespaces"):
            namespace_create_count += 1
            return httpx.Response(200, json=_cf_result({"id": "ns1", "title": "cloudflare-forwarding-defaults"}))
        if "/storage/kv/namespaces/ns1/values/" in path:
            key = path.rsplit("/", 1)[-1]
            if request.method == "GET":
                if key not in stored:
                    return httpx.Response(404)
                return httpx.Response(200, text=stored[key])
            if request.method == "PUT":
                stored[key] = request.content.decode()
                return httpx.Response(200, json=_cf_result(None))
            if request.method == "DELETE":
                stored.pop(key, None)
                return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    assert ops.kv_get("missing") is None
    ops.kv_put("alice--a1", '{"default": "allow"}')
    assert ops.kv_get("alice--a1") == '{"default": "allow"}'
    ops.kv_delete("alice--a1")
    assert ops.kv_get("alice--a1") is None
    # The missing-namespace branch must actually create the namespace (exactly once; the id
    # is cached after the first call).
    assert namespace_create_count == 1


def test_http_ops_kv_namespace_reuses_existing() -> None:
    """cf_kv_ensure_namespace returns the existing namespace's id without creating a new one."""
    create_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        path = request.url.path
        if request.method == "GET" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(
                200,
                json=_cf_result([{"id": "ns-existing", "title": "cloudflare-forwarding-defaults"}]),
            )
        if request.method == "POST" and path.endswith("/storage/kv/namespaces"):
            create_calls += 1
            return httpx.Response(200, json=_cf_result({"id": "ns-new", "title": "cloudflare-forwarding-defaults"}))
        if "/storage/kv/namespaces/ns-existing/values/" in path and request.method == "PUT":
            return httpx.Response(200, json=_cf_result(None))
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    ops = _build_http_ops_with_handler(handler)
    ops.kv_put("k", "v")
    assert create_calls == 0


def test_http_ops_service_token_roundtrip() -> None:
    """create_service_token, list_service_tokens, delete_service_token."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("POST", "/client/v4/accounts/acc/access/service_tokens"): httpx.Response(
            200, json=_cf_result({"id": "svc1", "client_id": "cid", "client_secret": "sec"})
        ),
        ("GET", "/client/v4/accounts/acc/access/service_tokens"): httpx.Response(
            200, json=_cf_result([{"id": "svc1"}])
        ),
        ("DELETE", "/client/v4/accounts/acc/access/service_tokens/svc1"): httpx.Response(200, json=_cf_result(None)),
    }
    ops = _build_http_ops_with_routes(routes)
    token = ops.create_service_token("name")
    assert token["id"] == "svc1"
    assert len(ops.list_service_tokens()) == 1
    ops.delete_service_token("svc1")


def test_ctx_set_tunnel_auth_is_persisted_in_kv() -> None:
    """set_tunnel_auth writes the JSON policy to the KV namespace keyed by tunnel name."""
    ctx = make_fake_forwarding_ctx()
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    stored_raw = ctx.fake.kv_get("alice--agent1")
    assert stored_raw is not None
    assert "a@b.com" in stored_raw


def test_ctx_remove_service_scrubs_ingress_rule() -> None:
    """Removing a service drops its hostname from the tunnel config's ingress."""
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.remove_service("alice--agent1", "alice", "web")
    config = ctx.fake.tunnel_configs[info.tunnel_id]
    hostnames = [r.get("hostname") for r in config["config"]["ingress"] if "hostname" in r]
    assert hostnames == []


def test_ctx_create_service_token_and_list() -> None:
    """create_service_token persists to the ops layer and returns a ServiceTokenInfo."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    token = ctx.create_service_token("alice--agent1", "alice", "svc-1")
    assert token.name == "svc-1"
    assert token.client_secret is not None
    # The created token is surfaced by list_service_tokens (which reflects the
    # ops layer), and the secret is never returned on a listing.
    listed = ctx.list_service_tokens()
    assert [t.name for t in listed] == ["svc-1"]
    assert listed[0].client_secret is None


# -- OVH cleanup tests --


def test_clean_up_pool_host_in_ovh_strips_tags_then_cancels() -> None:
    """The per-host OVH cleanup strips the stale tags and cancels by service name."""
    ovh_ops = FakeOvhOps()
    clean_up_pool_host_in_ovh(ovh_ops, "vps-test.vps.ovh.us", "us")
    expected_urn = vps_urn_for("vps-test.vps.ovh.us", "us")
    assert ovh_ops.deleted_tags == [(expected_urn, "minds_env"), (expected_urn, "mngr-host-id")]
    assert ovh_ops.cancelled == ["vps-test.vps.ovh.us"]


def test_cleanup_sweep_cleans_only_removing_rows() -> None:
    """The sweep cleans every 'removing' row and leaves leased/available rows alone."""
    backend = make_fake_pool_backend()
    removing = backend.add_removing_host(host_id=UUID("00000000-0000-0000-0000-0000000000a1"), version="v0.1.0")
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000a2"), version="v0.1.0", leased_to_user="someone"
    )
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-0000000000a3"), version="v0.1.0")

    conn = backend.get_connection()
    success_count, failure_count = run_pool_host_cleanup_sweep(conn, backend.ovh_ops, "us")

    assert (success_count, failure_count) == (1, 0)
    # The removing row is gone; the leased + available rows remain.
    remaining_ids = {row.host_id for row in backend.pool_rows}
    assert remaining_ids == {
        UUID("00000000-0000-0000-0000-0000000000a2"),
        UUID("00000000-0000-0000-0000-0000000000a3"),
    }
    assert backend.ovh_ops.cancelled == [removing.vps_instance_id]


def test_http_ovh_ops_set_delete_at_expiration_is_idempotent_for_gone_vps() -> None:
    """A 404 from OVH (VPS already removed) is treated as success, not an error."""

    class _NotFoundClient:
        def call(self, method: str, path: str, data: object, need_auth: bool) -> object:
            raise OvhResourceNotFoundError(f"{method} {path} not found")

    ops = HttpOvhOps(application_key="ak", application_secret="as", consumer_key="ck", endpoint="ovh-us")
    ops.client = _NotFoundClient()
    # The only observable contract here is "returns without raising": a missing service means
    # there is nothing left to cancel, so the 404 is swallowed with no side effect to assert.
    ops.set_delete_at_expiration("vps-gone.vps.ovh.us", True)


def test_cleanup_sweep_keeps_row_when_ovh_cancel_fails() -> None:
    """A removing row whose OVH cancel fails is kept for the next run."""
    backend = make_fake_pool_backend()
    backend.ovh_ops.fail_on_cancel = True
    backend.add_removing_host(host_id=UUID("00000000-0000-0000-0000-0000000000b1"), version="v0.1.0")

    conn = backend.get_connection()
    success_count, failure_count = run_pool_host_cleanup_sweep(conn, backend.ovh_ops, "us")

    assert (success_count, failure_count) == (0, 1)
    assert len(backend.pool_rows) == 1
    assert backend.pool_rows[0].status == "removing"


# -- PAID_ACCOUNT_SUFFIXES gate tests --
# -- Paid-list gate tests (paid_domains / paid_emails tables) --


def _paid_lookup_backend(
    *,
    paid_domains: tuple[str, ...] = (),
    paid_emails: tuple[str, ...] = (),
) -> Callable[[], Any]:
    """Build a connection factory over a fake backend seeded with the given lists."""
    backend = make_fake_pool_backend()
    for domain in paid_domains:
        backend.add_paid_domain(domain)
    for email in paid_emails:
        backend.add_paid_email(email)
    return backend.get_connection


@pytest.mark.parametrize(
    ("email", "paid_domains", "paid_emails", "expected"),
    [
        # Exact domain match (case-insensitive on both sides).
        ("alice@imbue.com", ("imbue.com",), (), True),
        ("ALICE@IMBUE.COM", ("imbue.com",), (), True),
        ("alice@imbue.com", ("IMBUE.COM",), (), True),
        # Subdomains do NOT match a bare-domain entry (exact match only).
        ("alice@eng.imbue.com", ("imbue.com",), (), False),
        ("alice@eng.imbue.com", ("eng.imbue.com",), (), True),
        # Full-email match.
        ("bob@gmail.com", (), ("bob@gmail.com",), True),
        ("eve@gmail.com", (), ("bob@gmail.com",), False),
        # Either list grants access.
        ("carol@imbue.com", ("imbue.com",), ("dave@elsewhere.com",), True),
        # Empty lists deny everyone.
        ("alice@imbue.com", (), (), False),
    ],
)
def test_is_email_paid_in_db_matching(
    email: str,
    paid_domains: tuple[str, ...],
    paid_emails: tuple[str, ...],
    expected: bool,
) -> None:
    factory = _paid_lookup_backend(paid_domains=paid_domains, paid_emails=paid_emails)
    assert is_email_paid_in_db(email, connection_factory=factory) is expected


def test_is_email_paid_in_db_ignores_soft_deleted_rows() -> None:
    backend = make_fake_pool_backend()
    backend.add_paid_email("bob@gmail.com", is_paid=False)
    backend.add_paid_domain("imbue.com", is_paid=False)
    assert is_email_paid_in_db("bob@gmail.com", connection_factory=backend.get_connection) is False
    assert is_email_paid_in_db("alice@imbue.com", connection_factory=backend.get_connection) is False


def test_is_email_paid_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a positive TTL, a second lookup is served from cache (db_lookup not re-invoked)."""
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "60")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    fake_clock = [1000.0]
    assert is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0]) is True
    # 30s later: still within the 60s window, so the cached value is reused.
    fake_clock[0] = 1030.0
    assert is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0]) is True
    assert call_count == 1
    clear_paid_status_cache()


def test_is_email_paid_refreshes_after_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "60")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    fake_clock = [1000.0]
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0])
    # 61s later: past the window, so the lookup runs again.
    fake_clock[0] = 1061.0
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0])
    assert call_count == 2
    clear_paid_status_cache()


def test_is_email_paid_bypasses_cache_when_ttl_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    is_email_paid("x@imbue.com", db_lookup=_counting_lookup)
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup)
    assert call_count == 2


def test_require_paid_account_allows_when_email_is_listed() -> None:
    # The only observable contract for the allow path is "returns without raising" (the guard
    # returns None); the raising branches are covered by the sibling tests below.
    require_paid_account(AdminAuth(username="alice", email="alice@imbue.com"), paid_checker=lambda email: True)


def test_require_paid_account_raises_403_when_email_not_listed() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_paid_account(
            AdminAuth(username="alice", email="alice@elsewhere.com"), paid_checker=lambda email: False
        )
    assert exc_info.value.status_code == 403
    assert "not authorized" in exc_info.value.detail


def test_require_paid_account_raises_403_when_email_is_none() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_paid_account(AdminAuth(username="alice", email=None), paid_checker=lambda email: True)
    assert exc_info.value.status_code == 403
    assert "email unavailable" in exc_info.value.detail


def test_require_paid_account_fails_closed_on_db_error() -> None:
    """A database error during the lookup denies access (403), never allows it."""

    def _raise_db_error(email: str) -> bool:
        raise psycopg2.OperationalError("connection refused")

    with pytest.raises(HTTPException) as exc_info:
        require_paid_account(AdminAuth(username="alice", email="alice@imbue.com"), paid_checker=_raise_db_error)
    assert exc_info.value.status_code == 403
    assert "database error" in exc_info.value.detail


# --- Unit tests for naming + helpers ---


def test_make_bucket_name_slugifies() -> None:
    assert make_bucket_name("user", "My Cool Data") == "user--my-cool-data"


def test_make_bucket_name_collapses_separators() -> None:
    assert make_bucket_name("user", "foo__bar--baz") == "user--foo-bar-baz"


def test_make_bucket_name_rejects_invalid() -> None:
    with pytest.raises(InvalidR2BucketNameError):
        make_bucket_name("user", "!!!")


def test_slugify_r2_name_strips_edges() -> None:
    assert slugify_r2_name("  --Foo--  ") == "foo"


def test_verify_bucket_ownership_rejects_foreign_prefix() -> None:
    with pytest.raises(R2BucketOwnershipError):
        verify_bucket_ownership("evil-user--x", "user")


def test_verify_bucket_ownership_accepts_owned() -> None:
    verify_bucket_ownership("user--x", "user")


def test_derive_s3_secret_matches_sha256() -> None:
    assert derive_s3_secret_access_key("hello") == hashlib.sha256(b"hello").hexdigest()


def test_r2_keys_migration_declares_all_persisted_columns() -> None:
    """Guard against the r2_keys schema and the PostgresKeyStore INSERT drifting apart."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "004_r2_keys.sql"
    migration_sql = migration_path.read_text()
    for column in ("access_key_id", "owner_user_id", "bucket_name", "access", "alias", "created_at"):
        # Word-boundary match so e.g. the bare "access" column is not spuriously
        # satisfied by the substring inside "access_key_id".
        assert re.search(rf"\b{re.escape(column)}\b", migration_sql), f"r2_keys migration is missing column {column!r}"


def test_paid_lists_migration_declares_both_tables() -> None:
    """Guard against the paid_domains / paid_emails schema drifting from the gate queries."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "005_paid_lists.sql"
    migration_sql = migration_path.read_text().lower()
    assert "create table paid_domains" in migration_sql
    assert "create table paid_emails" in migration_sql
    for column in ("is_paid", "created_at", "updated_at"):
        # Word-boundary match so a column is not spuriously satisfied by a substring of
        # another identifier or of comment prose.
        assert re.search(rf"\b{re.escape(column)}\b", migration_sql), (
            f"paid-lists migration is missing column {column!r}"
        )
