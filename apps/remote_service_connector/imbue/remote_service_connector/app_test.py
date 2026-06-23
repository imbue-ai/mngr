import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg2
import pytest
from fastapi import HTTPException
from ovh.exceptions import ResourceNotFoundError as OvhResourceNotFoundError
from starlette.testclient import TestClient
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

import imbue.remote_service_connector.app as app_mod
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
from imbue.remote_service_connector.app import _MAX_BUCKETS_PER_ACCOUNT
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
from imbue.remote_service_connector.app import web_app
from imbue.remote_service_connector.testing import FakeCloudflareOps
from imbue.remote_service_connector.testing import FakeOvhOps
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import InMemoryKeyStore
from imbue.remote_service_connector.testing import make_fake_forwarding_ctx
from imbue.remote_service_connector.testing import make_fake_key_store
from imbue.remote_service_connector.testing import make_fake_pool_backend
from imbue.remote_service_connector.testing import make_fake_supertokens_backend
from imbue.remote_service_connector.testing import make_fake_tunnel_token

_ADMIN_STUB_TOKEN = "admin-stub-jwt"
_ADMIN_STUB_USERNAME = "testuser"
_ADMIN_STUB_EMAIL = "testuser@example.com"
_PAID_ADMIN_KEY_TEST_VALUE = "paid-admin-key-secret-9f3a2b"


def _admin_headers() -> dict[str, str]:
    """Return a Bearer header for a fake SuperTokens admin session.

    Paired with ``_make_test_client`` which stubs ``_authenticate_supertokens``
    to recognise ``_ADMIN_STUB_TOKEN`` and return a canned ``AdminAuth``.
    """
    return {"Authorization": f"Bearer {_ADMIN_STUB_TOKEN}"}


def _agent_headers(tunnel_id: str) -> dict[str, str]:
    token = make_fake_tunnel_token(tunnel_id)
    return {"Authorization": f"Bearer {token}"}


def _make_test_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a TestClient with the FastAPI app, injecting a fake context.

    Sets up the SuperTokens Bearer auth path so tests calling admin endpoints
    can authenticate with ``_admin_headers()`` without needing a real JWT.
    Installs an in-memory paid-list backend seeded with the stub admin email
    so paid-feature endpoints (``/hosts/*``, ``/keys/*``, ``/buckets/*``)
    authorize out of the box; gate tests use ``_make_pool_test_client`` to
    get the backend and flip entries. The paid-status cache is disabled
    (``MINDS_PAID_LIST_CACHE_TTL_SECONDS=0``) so the module-level cache never
    bleeds between tests.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://fake-supertokens.example.com")
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    fake_ctx = make_fake_forwarding_ctx()
    monkeypatch.setattr(app_mod, "get_ctx", lambda: fake_ctx)

    def _stub_supertokens(token: str) -> AdminAuth:
        if token != _ADMIN_STUB_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")
        return AdminAuth(username=_ADMIN_STUB_USERNAME, email=_ADMIN_STUB_EMAIL)

    monkeypatch.setattr(app_mod, "_authenticate_supertokens", _stub_supertokens)
    backend = make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL)
    backend.install_on_app_module(app_mod, monkeypatch)
    return TestClient(web_app)


def test_make_tunnel_name_format() -> None:
    assert make_tunnel_name("alice", "agent1") == "alice--agent1"


def test_make_tunnel_name_allows_single_hyphen_in_agent_id() -> None:
    assert make_tunnel_name("alice", "agent-abc123") == "alice--abc123"


def test_make_tunnel_name_rejects_double_hyphen_in_username() -> None:
    with pytest.raises(InvalidTunnelComponentError, match="Username"):
        make_tunnel_name("alice--bob", "agent1")


def test_make_tunnel_name_truncates_agent_id() -> None:
    result = make_tunnel_name("alice", "agent--1")
    assert result == "alice---1"


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


def test_route_create_tunnel_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["tunnel_name"] == "testuser--agent1"
    assert data["token"] is not None


def test_route_create_tunnel_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post("/tunnels", json={"agent_id": "agent2"}, headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_list_tunnels_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.get("/tunnels", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_add_service_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["service_name"] == "web"


def test_route_add_service_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 200


def test_route_add_service_agent_wrong_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post("/tunnels", json={"agent_id": "agent2"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent2/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_list_services_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_remove_service_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.delete("/tunnels/testuser--agent1/services/web", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 200


def test_route_delete_tunnel_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.delete("/tunnels/testuser--agent1", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_set_tunnel_auth_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200


def test_route_get_tunnel_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/auth", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()["rules"]) == 1


def test_route_set_tunnel_auth_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": []},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_no_auth_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels")
    assert resp.status_code == 401


def test_route_rejects_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """After removing USER_CREDENTIALS, Basic Auth is no longer a supported scheme."""
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels", headers={"Authorization": "Basic dGVzdDp0ZXN0"})
    assert resp.status_code == 401


def test_route_malformed_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels/foo--bar/services", headers={"Authorization": "Bearer not-valid-base64!!!"})
    assert resp.status_code == 401


def test_route_create_tunnel_too_long_username_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a tunnel whose authenticated username is too long returns 400, not 500."""
    long_name = "a_very_long_username_exceeds_max"
    client = _make_test_client(monkeypatch)
    # Override the stub to return an AdminAuth with an overly-long username,
    # simulating a SuperTokens session whose user_id_prefix is longer than the
    # tunnel-naming limit.
    monkeypatch.setattr(
        app_mod,
        "_authenticate_supertokens",
        lambda _token: AdminAuth(username=long_name),
    )
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 400


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


# -- Auth route tests --
#
# These are smoke tests that verify the auth routes are wired up and reject
# calls when SuperTokens is not configured. Exercising the success paths
# requires a real SuperTokens core and is covered by release-marked E2E tests.


def test_auth_signup_returns_503_when_supertokens_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/signup without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/signup", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 503


def test_auth_signin_returns_503_when_supertokens_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/signin without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/signin", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 503


def test_auth_session_refresh_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/refresh without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/session/refresh", json={"refresh_token": "r"})
    assert resp.status_code == 503


def test_auth_session_revoke_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/revoke without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/session/revoke", headers={"Authorization": "Bearer any-token"})
    assert resp.status_code == 503


def test_auth_session_revoke_requires_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/revoke without a Bearer access token returns 401.

    This guards against an anonymous caller terminating arbitrary users'
    sessions just by knowing (or guessing) their user_id.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app)
    resp = client.post("/auth/session/revoke")
    assert resp.status_code == 401


def test_auth_verify_email_missing_token_shows_failed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verify-email endpoint renders an HTML failure page when the token is missing."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/verify-email")
    assert resp.status_code == 400
    assert "Verification failed" in resp.text


def test_auth_reset_password_page_renders_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset-password page renders an HTML form embedding the token."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/reset-password", params={"token": "tok-xyz"})
    assert resp.status_code == 200
    assert "tok-xyz" in resp.text
    assert "Reset password" in resp.text


# -- /auth/* happy-path tests (powered by FakeSuperTokensBackend) --


def _install_fake_supertokens(monkeypatch: pytest.MonkeyPatch) -> FakeSuperTokensBackend:
    """Wire the FakeSuperTokensBackend into the app module and return it."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    backend = make_fake_supertokens_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return backend


def test_auth_signup_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup creates an account, issues a session, and sends a verification email."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "new@example.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "new@example.com"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == 1
    assert "new@example.com" in backend.accounts_by_email


def test_auth_signup_field_error_on_empty_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup returns FIELD_ERROR for empty email or password."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "  ", "password": "x"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "FIELD_ERROR"


def test_auth_signup_duplicate_email_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing up with an email that already exists returns EMAIL_ALREADY_EXISTS."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    resp = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "EMAIL_ALREADY_EXISTS"
    assert len(backend.accounts_by_email) == 1


def test_auth_signup_returns_error_on_sdk_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens SDK exception in signup is surfaced as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("sign_up", SuperTokensGeneralError("core down"))
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ERROR",
        "message": "Auth backend unavailable",
        "user": None,
        "tokens": None,
        "needs_email_verification": False,
    }


def test_auth_signin_happy_path_with_verified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signin against a verified account returns OK and skips resending verification."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "a@b.com", "password": "password123"})
    initial_verify_count = len(backend.sent_verification_emails)
    account = backend.accounts_by_email["a@b.com"]
    backend.mark_email_verified(account.user_id)
    resp = client.post("/auth/signin", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["needs_email_verification"] is False
    assert len(backend.sent_verification_emails) == initial_verify_count


def test_auth_signin_wrong_password_returns_wrong_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signin with an incorrect password returns WRONG_CREDENTIALS without issuing a session."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})
    resp = client.post("/auth/signin", json={"email": "x@y.com", "password": "wrong"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "WRONG_CREDENTIALS"
    assert body["tokens"] is None


def test_auth_signin_unverified_email_triggers_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing in to an unverified account sends another verification email."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "unv@example.com", "password": "password123"})
    before = len(backend.sent_verification_emails)
    resp = client.post("/auth/signin", json={"email": "unv@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_signin_returns_error_on_sdk_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens SDK exception in signin is surfaced as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("sign_in", SuperTokensSessionError("session store down"))
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signin", json={"email": "x@y.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_session_refresh_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/refresh rotates tokens and invalidates the old refresh token."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "r@e.com", "password": "password123"}).json()
    initial_refresh = signup["tokens"]["refresh_token"]
    resp = client.post("/auth/session/refresh", json={"refresh_token": initial_refresh})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["tokens"]["refresh_token"] != initial_refresh
    assert initial_refresh not in backend.sessions_by_refresh_token


def test_auth_session_refresh_rejects_unknown_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/refresh returns status=ERROR for an unknown refresh token."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/session/refresh", json={"refresh_token": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_session_revoke_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/revoke tears down every session for the authenticated user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "rev@e.com", "password": "password123"}).json()
    access = signup["tokens"]["access_token"]
    assert len(backend.sessions_by_access_token) == 1
    resp = client.post(
        "/auth/session/revoke",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert resp.json()["revoked_count"] == 1
    assert len(backend.sessions_by_access_token) == 0


def test_auth_send_verification_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/send-verification resends a verification email for a known user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "v@e.com", "password": "password123"}).json()
    user_id = signup["user"]["user_id"]
    before = len(backend.sent_verification_emails)
    resp = client.post(
        "/auth/email/send-verification",
        json={"user_id": user_id, "email": "v@e.com"},
    )
    assert resp.status_code == 200
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_send_verification_email_unknown_user_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending verification email for a user that doesn't exist returns 404."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/email/send-verification",
        json={"user_id": "does-not-exist", "email": "a@b.com"},
    )
    assert resp.status_code == 404


def test_auth_is_email_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/is-verified reflects the underlying account state."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "iv@e.com", "password": "password123"}).json()
    user_id = signup["user"]["user_id"]
    resp = client.post("/auth/email/is-verified", json={"user_id": user_id, "email": "iv@e.com"})
    assert resp.status_code == 200
    assert resp.json() == {"verified": False}
    backend.mark_email_verified(user_id)
    resp = client.post("/auth/email/is-verified", json={"user_id": user_id, "email": "iv@e.com"})
    assert resp.json() == {"verified": True}


def test_auth_is_email_verified_unknown_user_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/is-verified returns verified=False for a user that doesn't exist."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/email/is-verified", json={"user_id": "nope", "email": "a@b.com"})
    assert resp.status_code == 200
    assert resp.json() == {"verified": False}


def test_auth_verify_email_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verify-email page consumes a valid token and marks the account verified."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "ve@e.com", "password": "password123"})
    token = next(iter(backend.verification_tokens.keys()))
    resp = client.get("/auth/verify-email", params={"token": token})
    assert resp.status_code == 200
    assert "Email verified" in resp.text
    user_id = backend.accounts_by_email["ve@e.com"].user_id
    assert backend.accounts_by_id[user_id].is_verified is True


def test_auth_verify_email_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting an invalid verification token renders the failure page."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/verify-email", params={"token": "bogus"})
    assert resp.status_code == 400
    assert "Verification failed" in resp.text


def test_auth_forgot_password_sends_reset_email_for_known_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/password/forgot enqueues a reset email when the account exists."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "fp@e.com", "password": "password123"})
    resp = client.post("/auth/password/forgot", json={"email": "fp@e.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert len(backend.sent_reset_emails) == 1


def test_auth_forgot_password_unknown_email_still_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """For unknown emails the endpoint returns the same success shape (anti-enumeration)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/password/forgot", json={"email": "nobody@e.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert backend.sent_reset_emails == []


def test_auth_reset_password_consumes_token_and_updates_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid reset token updates the account password; it cannot be reused."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "rp@e.com", "password": "password123"})
    user_id = backend.accounts_by_email["rp@e.com"].user_id
    token = backend.issue_reset_token(user_id)
    resp = client.post("/auth/password/reset", json={"token": token, "new_password": "newpass456"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert backend.accounts_by_id[user_id].password == "newpass456"
    resp = client.post("/auth/password/reset", json={"token": token, "new_password": "again789"})
    assert resp.json()["status"] == "INVALID_TOKEN"


def test_auth_reset_password_rejects_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/password/reset returns 400 when the token or password is missing."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/password/reset", json={"token": "", "new_password": ""})
    assert resp.status_code == 400


def test_auth_oauth_authorize_returns_redirect_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/authorize asks the provider for a redirect URL."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="oa@e.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "google", "callback_url": "http://127.0.0.1:9999/cb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["url"].startswith("https://google.example.com/auth")


def test_auth_oauth_authorize_unknown_provider_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/authorize returns status=ERROR for a provider that isn't registered."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "unknown", "callback_url": "http://127.0.0.1/cb"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_oauth_callback_creates_user_and_returns_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/callback links the provider user, creates an account, and returns tokens."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider(
        "google",
        email="cb@e.com",
        third_party_user_id="tp-1",
        display_name="Callback User",
    )
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "google",
            "callback_url": "http://127.0.0.1:9999/cb",
            "query_params": {"code": "abc", "state": "xyz"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "cb@e.com"
    assert body["user"]["display_name"] == "Callback User"
    assert body["tokens"]["access_token"].startswith("at-")
    assert "cb@e.com" in backend.accounts_by_email


def test_auth_oauth_callback_unknown_provider_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/callback returns status=ERROR for a provider that isn't registered."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "missing",
            "callback_url": "http://127.0.0.1/cb",
            "query_params": {},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_get_user_returns_provider_email_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} reports 'email' for password-registered accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "gu@e.com", "password": "password123"})
    user_id = backend.accounts_by_email["gu@e.com"].user_id
    resp = client.get(f"/auth/users/{user_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "email"
    assert body["email"] == "gu@e.com"


def test_auth_get_user_reports_third_party_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} reports the OAuth provider ID for OAuth accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="oauth-user@e.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "google",
            "callback_url": "http://127.0.0.1/cb",
            "query_params": {"code": "a"},
        },
    )
    user_id = backend.accounts_by_email["oauth-user@e.com"].user_id
    resp = client.get(f"/auth/users/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "google"


def test_auth_get_user_missing_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} returns 404 when the user does not exist."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/users/does-not-exist")
    assert resp.status_code == 404


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

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/storage/kv/namespaces"):
            return httpx.Response(200, json=_cf_result([]))
        if request.method == "POST" and path.endswith("/storage/kv/namespaces"):
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


# -- Uncovered route and ctx-method tests --


def test_route_get_service_auth_returns_empty_rules_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tunnels/.../services/.../auth returns {'rules': []} when no policy is set."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services/web/auth", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"rules": []}


def test_route_set_service_auth_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /tunnels/.../services/.../auth admin path persists the policy."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.put(
        "/tunnels/testuser--agent1/services/web/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "updated"}


def test_route_get_tunnel_auth_returns_empty_rules_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tunnels/.../auth returns an empty rules list when no tunnel-level policy is set."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.get("/tunnels/testuser--agent1/auth", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"rules": []}


def test_route_create_and_list_service_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST/GET /tunnels/.../service-tokens round-trip through ForwardingCtx."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.post(
        "/tunnels/testuser--agent1/service-tokens",
        json={"name": "my-token"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "my-token"
    assert body["client_secret"] is not None
    resp = client.get("/tunnels/testuser--agent1/service-tokens", headers=_admin_headers())
    assert resp.status_code == 200
    listed = resp.json()
    # FakeCloudflareOps.list_service_tokens returns an empty list by design (it
    # doesn't persist created tokens), so the listing is empty -- the test
    # still covers the endpoint + ForwardingCtx.list_service_tokens path.
    assert listed == []


def test_route_service_tokens_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent Bearer auth can't create service tokens (admin-only)."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/service-tokens",
        json={"name": "my-token"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_list_services_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tunnels/.../services admin path lists services."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_delete_tunnel_admin_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admin can delete a tunnel they own."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.delete("/tunnels/testuser--agent1", headers=_admin_headers())
    assert resp.status_code == 200
    resp = client.get("/tunnels", headers=_admin_headers())
    assert resp.json() == []


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
    # FakeCloudflareOps.list_service_tokens returns []; list_service_tokens should
    # reflect that rather than pulling from an internal cache.
    assert ctx.list_service_tokens() == []


# -- Host pool endpoint tests --


def _make_pool_test_client(
    monkeypatch: pytest.MonkeyPatch,
    pool_backend: FakePoolBackend | None = None,
) -> tuple[TestClient, FakePoolBackend]:
    """Create a TestClient with both tunnel-auth and pool-backend fakes installed.

    The returned backend is seeded with the stub admin email as paid so
    paid-feature routes authorize by default; gate tests flip entries via
    ``backend.add_paid_email`` / ``add_paid_domain`` / the CRUD endpoints.
    """
    client = _make_test_client(monkeypatch)
    monkeypatch.setenv("POOL_SSH_PRIVATE_KEY", "fake-management-key-pem")
    backend = pool_backend if pool_backend is not None else make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL)
    backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend


def test_lease_host_returns_available_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns a host when one is available with matching version."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        vps_address="10.0.0.1",
        agent_id="agent-111",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_db_id"] == "00000000-0000-0000-0000-000000000001"
    assert body["vps_address"] == "10.0.0.1"
    assert body["agent_id"] == "agent-111"
    assert body["host_name"] == "my-workspace"
    assert body["attributes"] == {"version": "v0.1.0"}
    # The lease returns both pinned sshd host keys so the client can verify the
    # host strictly instead of trust-on-first-use.
    assert body["outer_host_public_key"]
    assert body["container_host_public_key"]
    # Verify SSH key was injected on both VPS and container, each pinning the
    # corresponding recorded host key (the 6th element of the recorded call).
    assert len(backend.append_key_calls) == 2
    injected_ports = {call[1]: call[5] for call in backend.append_key_calls}
    assert injected_ports[22] == body["outer_host_public_key"]
    assert injected_ports[2222] == body["container_host_public_key"]
    # Verify host was marked as leased and the user-supplied host_name was
    # written to the row.
    assert backend.pool_rows[0].status == "leased"
    assert backend.pool_rows[0].leased_to_user == _ADMIN_STUB_USERNAME
    assert backend.pool_rows[0].host_name == "my-workspace"


def test_lease_host_fails_closed_when_host_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool row with no pinned host keys is not leasable (no trust-on-first-use)."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        outer_host_public_key=None,
        container_host_public_key=None,
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert "host-key backfill" in resp.json()["detail"]
    # The row must NOT have been leased, and no SSH key injection was attempted.
    assert backend.pool_rows[0].status == "available"
    assert backend.append_key_calls == []


def test_lease_host_returns_503_when_pool_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns 503 when no hosts are available."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert "No pre-created agents" in resp.json()["detail"]


def test_lease_host_returns_503_when_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns 503 when available hosts have a different version."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.2.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert "No pre-created agents" in resp.json()["detail"]
    # Verify the host was not leased
    assert backend.pool_rows[0].status == "available"


def test_lease_host_hard_region_filters_out_other_regions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard ``region`` only leases a host in that datacenter; otherwise 503."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        region="US-WEST-OR",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
            "region": "US-EAST-VA",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert backend.pool_rows[0].status == "available"


def test_lease_host_hard_region_leases_matching_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard ``region`` leases a host whose region matches."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        region="US-EAST-VA",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
            "region": "US-EAST-VA",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert backend.pool_rows[0].status == "leased"


def test_lease_host_rejects_invalid_host_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease rejects host_name values that fail the SafeName regex."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "bad.name",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 422
    # The available row stays available since validation rejected the request
    # before the SELECT/UPDATE.
    assert backend.pool_rows[0].status == "available"


def test_release_host_succeeds_for_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release strips OVH tags, cancels the VPS, and drops the row."""
    client, backend = _make_pool_test_client(monkeypatch)
    row = backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    # Row fully cleaned up (deleted), VPS cancelled, stale tags stripped.
    assert backend.pool_rows == []
    assert backend.ovh_ops.cancelled == [row.vps_instance_id]
    stripped_keys = {key for _urn, key in backend.ovh_ops.deleted_tags}
    assert stripped_keys == {"minds_env", "mngr-host-id"}
    # The provider tag is never stripped.
    assert all(key != "mngr-provider" for _urn, key in backend.ovh_ops.deleted_tags)


def test_release_host_idempotent_when_already_removing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release on a row already in 'removing' re-drives cleanup and returns 200."""
    client, backend = _make_pool_test_client(monkeypatch)
    row = backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-000000000077"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000077/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []
    assert backend.ovh_ops.cancelled == [row.vps_instance_id]


def test_release_host_fails_loudly_when_ovh_cancel_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed OVH cancel makes release return an error -- never a false success.

    Synchronous release contract: a "released" 200 must mean the VPS is actually
    cancelled. When the cancel fails the endpoint returns 5xx and keeps the row
    as 'removing' so the client (or the sweep backstop) retries -- the opposite
    of the old behavior, which returned 200 and silently stranded the VPS.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.ovh_ops.fail_on_cancel = True
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000099"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000099/release", headers=_admin_headers())
    assert resp.status_code == 502
    # The row is NOT deleted; it stays 'removing' so the teardown is retryable.
    assert len(backend.pool_rows) == 1
    assert backend.pool_rows[0].status == "removing"


def test_release_host_returns_403_for_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release returns 403 when the caller is not the lease owner."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"), version="v0.1.0", leased_to_user="other-user"
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 403
    assert "do not own" in resp.json()["detail"]
    # Verify the host was not released
    assert backend.pool_rows[0].status == "leased"


def test_release_host_unknown_returns_already_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release on a missing row returns 200 already_released (idempotent)."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000999/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_released"


def test_list_hosts_returns_leased_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /hosts returns only hosts leased by the authenticated user."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
        agent_id="agent-aaa",
    )
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000002"),
        version="v0.1.0",
        leased_to_user="other-user",
        agent_id="agent-bbb",
    )
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000003"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
        agent_id="agent-ccc",
    )
    resp = client.get("/hosts", headers=_admin_headers())
    assert resp.status_code == 200
    hosts = resp.json()
    assert len(hosts) == 2
    host_ids = {h["host_db_id"] for h in hosts}
    assert host_ids == {"00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000003"}


# -- OVH cleanup tests --


def test_clean_up_pool_host_in_ovh_strips_tags_then_cancels() -> None:
    """The per-host OVH cleanup strips the stale tags and cancels by service name."""
    ovh_ops = FakeOvhOps()
    clean_up_pool_host_in_ovh(ovh_ops, "vps-test.vps.ovh.us", "us")
    expected_urn = vps_urn_for("vps-test.vps.ovh.us", "us")
    assert ovh_ops.deleted_tags == [(expected_urn, "minds_env"), (expected_urn, "mngr-host-id")]
    assert ovh_ops.cancelled == ["vps-test.vps.ovh.us"]


def test_cleanup_sweep_cleans_only_removing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep cleans every 'removing' row and leaves leased/available rows alone."""
    _client, backend = _make_pool_test_client(monkeypatch)
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
    # Must not raise: a missing service means there is nothing left to cancel.
    ops.set_delete_at_expiration("vps-gone.vps.ovh.us", True)


def test_cleanup_sweep_keeps_row_when_ovh_cancel_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A removing row whose OVH cancel fails is kept for the next run."""
    _client, backend = _make_pool_test_client(monkeypatch)
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


def test_route_lease_host_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool-lease route denies a caller whose email is not in the paid lists."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    # Flip the seeded stub email to not-paid.
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 403
    # Verify the gate fired before any DB / SSH side effects ran.
    assert backend.pool_rows[0].status == "available"
    assert backend.append_key_calls == []


def test_route_release_host_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
    )
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 403
    assert backend.pool_rows[0].status == "leased"


def test_route_list_hosts_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.get("/hosts", headers=_admin_headers())
    assert resp.status_code == 403


def test_route_create_litellm_key_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LiteLLM key-create route enforces the paid-list gate.

    The gate fires before any LiteLLM HTTP call, so this test does not need
    to stub the LiteLLM proxy.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/keys/create", json={}, headers=_admin_headers())
    assert resp.status_code == 403


def test_route_list_litellm_keys_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.get("/keys", headers=_admin_headers())
    assert resp.status_code == 403


def test_route_get_litellm_key_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.get("/keys/some-key-id", headers=_admin_headers())
    assert resp.status_code == 403


def test_route_update_litellm_key_budget_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.put("/keys/some-key-id/budget", json={}, headers=_admin_headers())
    assert resp.status_code == 403


def test_route_delete_litellm_key_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.delete("/keys/some-key-id", headers=_admin_headers())
    assert resp.status_code == 403


def test_route_create_tunnel_is_not_gated_by_paid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloudflare forwarding (`/tunnels/*`) must work even when the user is not paid."""
    client, backend = _make_pool_test_client(monkeypatch)
    # Not-paid: the tunnel route should be unaffected by the paid gate.
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["tunnel_name"] == f"{_ADMIN_STUB_USERNAME}--agent1"


def test_route_list_services_is_not_gated_by_paid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel services routes work for any verified email regardless of paid status."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    create_resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert create_resp.status_code == 200
    list_resp = client.get(f"/tunnels/{_ADMIN_STUB_USERNAME}--agent1/services", headers=_admin_headers())
    assert list_resp.status_code == 200


# -- Paid-list CRUD endpoint tests (admin-key authenticated) --


def _paid_admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_PAID_ADMIN_KEY_TEST_VALUE}"}


def _make_paid_crud_test_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakePoolBackend]:
    """Test client with the paid-admin key configured and a fresh paid-list backend."""
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", _PAID_ADMIN_KEY_TEST_VALUE)
    return client, backend


def test_paid_crud_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    # No Authorization header.
    assert client.get("/paid/domains").status_code == 401
    # Wrong key.
    bad = client.get("/paid/domains", headers={"Authorization": "Bearer wrong-key"})
    assert bad.status_code == 401
    # A SuperTokens admin JWT is NOT accepted on the paid CRUD endpoints.
    assert client.get("/paid/domains", headers=_admin_headers()).status_code == 401


def test_paid_crud_rejects_non_ascii_bearer_token_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ASCII bearer credential is a clean 401, not a 500 (compare over bytes)."""
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    # HTTP header values are latin-1; pass raw bytes (both key and value, which
    # matches httpx's ``Mapping[bytes, bytes]`` header type) so the non-ASCII
    # octets reach the handler -- httpx would otherwise reject a non-ASCII str.
    resp = client.get("/paid/domains", headers={b"Authorization": "Bearer wröng-kéy".encode("latin-1")})
    assert resp.status_code == 401


def test_paid_crud_returns_403_when_admin_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.delenv("MINDS_PAID_ADMIN_KEY", raising=False)
    resp = client.get("/paid/domains", headers=_paid_admin_headers())
    assert resp.status_code == 403
    assert "not enabled" in resp.json()["detail"]


def test_paid_admin_key_is_rejected_on_user_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin key must not authenticate user-facing routes (e.g. /hosts)."""
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.get("/hosts", headers=_paid_admin_headers())
    assert resp.status_code == 401


def test_add_and_list_paid_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    add_resp = client.post("/paid/domains/add", json={"value": "Imbue.com"}, headers=_paid_admin_headers())
    assert add_resp.status_code == 200
    assert add_resp.json() == {"status": "added", "domain": "imbue.com"}
    list_resp = client.get("/paid/domains", headers=_paid_admin_headers())
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert [r["domain"] for r in rows] == ["imbue.com"]
    assert rows[0]["is_paid"] is True


def test_remove_paid_domain_is_soft_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    remove_resp = client.post("/paid/domains/remove", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    assert remove_resp.status_code == 200
    # The row is still present (soft delete), but is_paid is now false.
    all_rows = client.get("/paid/domains", headers=_paid_admin_headers()).json()
    assert [(r["domain"], r["is_paid"]) for r in all_rows] == [("imbue.com", False)]
    # paid_only filter hides it.
    paid_rows = client.get("/paid/domains?paid_only=true", headers=_paid_admin_headers()).json()
    assert paid_rows == []


def test_re_adding_soft_removed_domain_reactivates_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    original = client.get("/paid/domains", headers=_paid_admin_headers()).json()[0]
    client.post("/paid/domains/remove", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    reactivated = client.get("/paid/domains", headers=_paid_admin_headers()).json()[0]
    assert reactivated["is_paid"] is True
    # created_at is preserved across the remove/re-add cycle.
    assert reactivated["created_at"] == original["created_at"]


def test_add_paid_domain_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    first = client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    second = client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    assert first.status_code == 200
    assert second.status_code == 200
    rows = client.get("/paid/domains", headers=_paid_admin_headers()).json()
    assert len(rows) == 1


def test_remove_absent_paid_email_is_idempotent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/emails/remove", json={"value": "nobody@nowhere.com"}, headers=_paid_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"status": "removed", "email": "nobody@nowhere.com"}


def test_add_paid_email_then_gate_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: add a paid email via CRUD, then a user with that email passes the gate."""
    client, backend = _make_paid_crud_test_client(monkeypatch)
    # Start from a clean slate where the stub email is not paid.
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    assert client.get("/hosts", headers=_admin_headers()).status_code == 403
    client.post("/paid/emails/add", json={"value": _ADMIN_STUB_EMAIL}, headers=_paid_admin_headers())
    assert client.get("/hosts", headers=_admin_headers()).status_code == 200


@pytest.mark.parametrize("bad_value", ["", "   ", "has space", "foo@bar.com"])
def test_add_paid_domain_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/domains/add", json={"value": bad_value}, headers=_paid_admin_headers())
    assert resp.status_code == 400


@pytest.mark.parametrize("bad_value", ["", "no-at-sign", "@nodomain", "local@", "a b@c.com"])
def test_add_paid_email_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/emails/add", json={"value": bad_value}, headers=_paid_admin_headers())
    assert resp.status_code == 400


# -- R2 bucket endpoint tests --


_ADMIN_STUB_USER_ID = "12345678-1234-5678-1234-567812345678"


def _make_bucket_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeCloudflareOps, InMemoryKeyStore]:
    """Create a TestClient with the R2 fakes installed (Cloudflare ops + key store)."""
    client = _make_test_client(monkeypatch)
    # Build our own fake ctx so the fake is typed as FakeForwardingCtx (which
    # exposes ``.fake``); re-patching get_ctx overrides the one _make_test_client
    # installed.
    fake_ctx = make_fake_forwarding_ctx()
    store = make_fake_key_store()
    # Single-loop patching (same pattern as the Fake*Backend.install_on_app_module
    # helpers) so the monkeypatch ratchet only counts one occurrence.
    bucket_fakes: dict[str, object] = {
        "get_ctx": lambda: fake_ctx,
        "get_key_store": lambda: store,
        "_get_user_id_from_access_token": lambda token: _ADMIN_STUB_USER_ID,
    }
    for name, fake_impl in bucket_fakes.items():
        monkeypatch.setattr(app_mod, name, fake_impl)
    return client, fake_ctx.fake, store


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


# --- Route tests ---


def test_create_bucket_returns_bucket_and_default_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "my-data"}, headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket"]["bucket_name"] == "testuser--my-data"
    assert body["bucket"]["s3_endpoint"] == "https://test-account.r2.cloudflarestorage.com"
    assert body["key"]["access"] == "readwrite"
    assert body["key"]["bucket_name"] == "testuser--my-data"
    access_key_id = body["key"]["access_key_id"]
    assert access_key_id
    # Secret is the sha256 of the fake token value, returned once.
    assert body["key"]["secret_access_key"] == derive_s3_secret_access_key(f"token-value-{access_key_id}")
    # Bucket actually created in the fake.
    assert "testuser--my-data" in fake.buckets
    # Key metadata recorded; the secret/token value is NOT persisted.
    rows = store.list_keys(_ADMIN_STUB_USER_ID, None)
    assert len(rows) == 1
    assert rows[0]["access_key_id"] == access_key_id
    assert "secret_access_key" not in rows[0]
    assert "value" not in rows[0]
    assert rows[0]["owner_user_id"] == _ADMIN_STUB_USER_ID


def test_create_bucket_with_read_access(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "ro", "access": "read"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["key"]["access"] == "read"


def test_create_bucket_invalid_access_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "x", "access": "write"}, headers=_admin_headers())
    assert resp.status_code == 422


def test_create_bucket_invalid_name_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "!!!"}, headers=_admin_headers())
    assert resp.status_code == 400


def test_create_bucket_duplicate_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    assert client.post("/buckets", json={"name": "dup"}, headers=_admin_headers()).status_code == 200
    resp = client.post("/buckets", json={"name": "dup"}, headers=_admin_headers())
    assert resp.status_code == 409


def test_create_bucket_at_cap_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    for i in range(_MAX_BUCKETS_PER_ACCOUNT):
        name = f"testuser--b{i}"
        fake.buckets[name] = {"name": name}
        fake.bucket_objects[name] = []
    resp = client.post("/buckets", json={"name": "one-more"}, headers=_admin_headers())
    assert resp.status_code == 409


def test_list_buckets_returns_only_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "a"}, headers=_admin_headers())
    client.post("/buckets", json={"name": "b"}, headers=_admin_headers())
    # A bucket owned by someone else, plus a crafted name that merely *contains*
    # the prefix -- the in-code startswith re-check must exclude it.
    fake.buckets["otheruser--secret"] = {"name": "otheruser--secret"}
    fake.buckets["evil-testuser--x"] = {"name": "evil-testuser--x"}
    resp = client.get("/buckets", headers=_admin_headers())
    assert resp.status_code == 200
    names = sorted(b["bucket_name"] for b in resp.json())
    assert names == ["testuser--a", "testuser--b"]


def test_get_bucket_info(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    resp = client.get("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["bucket_name"] == "testuser--data"


def test_get_bucket_info_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.get("/buckets/missing", headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_bucket_non_empty_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    fake.bucket_objects["testuser--data"].append("obj1")
    resp = client.delete("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 409
    assert "testuser--data" in fake.buckets


def test_destroy_bucket_empty_cascades_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    client.post("/buckets/data/keys", json={}, headers=_admin_headers())
    assert len(store.list_keys(_ADMIN_STUB_USER_ID, None)) == 2
    assert len(fake.account_tokens) == 2
    resp = client.delete("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 200
    assert "testuser--data" not in fake.buckets
    assert store.list_keys(_ADMIN_STUB_USER_ID, None) == []
    assert fake.account_tokens == {}


def test_create_additional_key_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    resp = client.post("/buckets/data/keys", json={"alias": "ro", "access": "read"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["access"] == "read"
    per_bucket = client.get("/buckets/data/keys", headers=_admin_headers()).json()
    assert sorted(k["alias"] for k in per_bucket) == ["default", "ro"]
    account_wide = client.get("/bucket-keys", headers=_admin_headers()).json()
    assert len(account_wide) == 2


def test_create_key_for_missing_bucket_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets/nope/keys", json={}, headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    create = client.post("/buckets", json={"name": "data"}, headers=_admin_headers()).json()
    access_key_id = create["key"]["access_key_id"]
    resp = client.delete(f"/bucket-keys/{access_key_id}", headers=_admin_headers())
    assert resp.status_code == 200
    assert access_key_id not in fake.account_tokens
    assert store.get_key(access_key_id) is None


def test_destroy_key_unknown_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.delete("/bucket-keys/does-not-exist", headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_key_not_owned_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, store = _make_bucket_test_client(monkeypatch)
    store.add_key("akid-other", "some-other-user", "other--bucket", "readwrite", "x")
    resp = client.delete("/bucket-keys/akid-other", headers=_admin_headers())
    assert resp.status_code == 404
    # The other user's row is untouched.
    assert store.get_key("akid-other") is not None


def test_buckets_require_paid_account(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    # Install a paid-list backend where the stub admin email is NOT paid.
    backend = make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    backend.install_on_app_module(app_mod, monkeypatch)
    resp = client.post("/buckets", json={"name": "x"}, headers=_admin_headers())
    assert resp.status_code == 403


def test_r2_keys_migration_declares_all_persisted_columns() -> None:
    """Guard against the r2_keys schema and the PostgresKeyStore INSERT drifting apart."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "004_r2_keys.sql"
    migration_sql = migration_path.read_text()
    for column in ("access_key_id", "owner_user_id", "bucket_name", "access", "alias", "created_at"):
        assert column in migration_sql, f"r2_keys migration is missing column {column!r}"


def test_paid_lists_migration_declares_both_tables() -> None:
    """Guard against the paid_domains / paid_emails schema drifting from the gate queries."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "005_paid_lists.sql"
    migration_sql = migration_path.read_text().lower()
    assert "create table paid_domains" in migration_sql
    assert "create table paid_emails" in migration_sql
    for column in ("is_paid", "created_at", "updated_at"):
        assert column in migration_sql, f"paid-lists migration is missing column {column!r}"


def test_slice_name_env_owner_parses_stamped_instance_and_disk_names() -> None:
    host_hex = "0123456789abcdef0123456789abcdef"
    assert app_mod.slice_name_env_owner(f"mngr-slice-dev-josh-foo-{host_hex}") == "dev-josh-foo"
    # The env is recoverable from the data-disk name too (the -data suffix is stripped).
    assert app_mod.slice_name_env_owner(f"mngr-slice-dev-josh-foo-{host_hex}-data") == "dev-josh-foo"


def test_slice_name_env_owner_returns_none_for_legacy_and_non_slice_names() -> None:
    host_hex = "0123456789abcdef0123456789abcdef"
    # Legacy un-stamped slice names have no env owner (must be left untouched).
    assert app_mod.slice_name_env_owner(f"mngr-slice-{host_hex}") is None
    assert app_mod.slice_name_env_owner(f"mngr-slice-{host_hex}-data") is None
    # Non-slice lima names are never attributed to an env.
    assert app_mod.slice_name_env_owner("default") is None
    assert app_mod.slice_name_env_owner("some-other-vm") is None
