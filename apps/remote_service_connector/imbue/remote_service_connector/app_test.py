import json
from collections.abc import Callable

import httpx
import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

import imbue.remote_service_connector.app as app_mod
from imbue.remote_service_connector.app import AdminAuth
from imbue.remote_service_connector.app import AuthPolicy
from imbue.remote_service_connector.app import CloudflareApiError
from imbue.remote_service_connector.app import HttpCloudflareOps
from imbue.remote_service_connector.app import InvalidTunnelComponentError
from imbue.remote_service_connector.app import ServiceNotFoundError
from imbue.remote_service_connector.app import TunnelComponentTooLongError
from imbue.remote_service_connector.app import TunnelNotFoundError
from imbue.remote_service_connector.app import TunnelOwnershipError
from imbue.remote_service_connector.app import _authenticate_supertokens
from imbue.remote_service_connector.app import cf_check
from imbue.remote_service_connector.app import cf_list_all_pages
from imbue.remote_service_connector.app import extract_service_name
from imbue.remote_service_connector.app import extract_username_from_tunnel_name
from imbue.remote_service_connector.app import make_hostname
from imbue.remote_service_connector.app import make_tunnel_name
from imbue.remote_service_connector.app import web_app
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import make_fake_forwarding_ctx
from imbue.remote_service_connector.testing import make_fake_supertokens_backend
from imbue.remote_service_connector.testing import make_fake_tunnel_token

_ADMIN_STUB_TOKEN = "admin-stub-jwt"
_ADMIN_STUB_USERNAME = "testuser"


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
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://fake-supertokens.example.com")
    fake_ctx = make_fake_forwarding_ctx()
    monkeypatch.setattr(app_mod, "get_ctx", lambda: fake_ctx)

    def _stub_supertokens(token: str) -> AdminAuth:
        if token != _ADMIN_STUB_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")
        return AdminAuth(username=_ADMIN_STUB_USERNAME)

    monkeypatch.setattr(app_mod, "_authenticate_supertokens", _stub_supertokens)
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
    # The tunnel-wide Access app and any per-service override apps are
    # cascaded away with the tunnel.
    assert len(ctx.fake.access_apps) == 0


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


def test_add_service_adds_to_tunnel_wide_app_self_hosted_domains() -> None:
    """Adding a service appends its hostname to the tunnel-wide Access app's
    ``self_hosted_domains`` instead of creating a new per-service app."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    # Just the tunnel-wide app exists so far, covering the predicted
    # system_interface hostname.
    assert len(ctx.fake.access_apps) == 1
    tunnel_app = next(iter(ctx.fake.access_apps.values()))
    assert tunnel_app["self_hosted_domains"] == ["system_interface--agent1--alice.example.com"]

    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    # Still exactly one Access app; the new hostname just gets added to its coverage.
    assert len(ctx.fake.access_apps) == 1
    assert tunnel_app["self_hosted_domains"] == [
        "system_interface--agent1--alice.example.com",
        "web--agent1--alice.example.com",
    ]


def test_create_tunnel_passes_allowed_idps_to_tunnel_wide_app() -> None:
    """When ForwardingCtx has allowed_idps configured, the tunnel-wide Access
    app is created with them."""
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp-uuid-123"])
    ctx.create_tunnel("alice", "agent1")
    tunnel_app = next(iter(ctx.fake.access_apps.values()))
    assert tunnel_app["allowed_idps"] == ["google-idp-uuid-123"]


def test_create_tunnel_no_allowed_idps_when_not_configured() -> None:
    """When allowed_idps is None, it is not included in the tunnel-wide Access app."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    tunnel_app = next(iter(ctx.fake.access_apps.values()))
    assert "allowed_idps" not in tunnel_app


def test_set_service_auth_creates_override_app_with_allowed_idps() -> None:
    """set_service_auth creates a per-hostname override Access app (separate
    from the tunnel-wide app) with allowed_idps when configured."""
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp-uuid-123", "otp-idp-uuid-456"])
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", policy)
    # Tunnel-wide app + new override app.
    assert len(ctx.fake.access_apps) == 2
    override_apps = [a for a in ctx.fake.access_apps.values() if a["name"].startswith("override-")]
    assert len(override_apps) == 1
    assert override_apps[0]["allowed_idps"] == ["google-idp-uuid-123", "otp-idp-uuid-456"]
    assert override_apps[0]["domain"] == "web--agent1--alice.example.com"


def test_remove_service_drops_from_tunnel_app_and_deletes_override() -> None:
    """Removing a service with an override app: removes it from the tunnel-wide
    app's self_hosted_domains and deletes the override app. The tunnel-wide
    app itself stays."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", policy)
    # Tunnel-wide app + newly created override app.
    assert len(ctx.fake.access_apps) == 2

    ctx.remove_service("alice--agent1", "alice", "web")
    # Override gone, tunnel-wide app persists but no longer lists the hostname.
    assert len(ctx.fake.access_apps) == 1
    tunnel_app = next(iter(ctx.fake.access_apps.values()))
    assert "web--agent1--alice.example.com" not in tunnel_app["self_hosted_domains"]


def test_remove_service_without_override_just_drops_hostname() -> None:
    """Removing a service that has no override app just removes its hostname
    from the tunnel-wide app's coverage."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    # Just the tunnel-wide app; no per-service override was created.
    assert len(ctx.fake.access_apps) == 1

    ctx.remove_service("alice--agent1", "alice", "web")
    # Same one app, hostname no longer in coverage.
    assert len(ctx.fake.access_apps) == 1
    tunnel_app = next(iter(ctx.fake.access_apps.values()))
    assert "web--agent1--alice.example.com" not in tunnel_app["self_hosted_domains"]


def test_set_service_auth_on_primary_creates_distinct_override_app() -> None:
    """``set_service_auth`` on the primary (``system_interface``) hostname must
    create a NEW override app, not repurpose the tunnel-wide app.

    The tunnel-wide app's ``domain`` equals the primary hostname, so a naive
    by-domain fallback in ``_get_override_app`` would return the tunnel-wide
    app for the primary hostname and silently clobber its default policy.
    """
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "system_interface", "http://localhost:8080")
    # Capture the tunnel-wide app id + policy count before the override.
    tunnel_app = next(iter(ctx.fake.access_apps.values()))
    tunnel_app_id = tunnel_app["id"]
    policies_before = list(ctx.fake.access_policies.get(tunnel_app_id, []))

    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "system_interface", policy)

    # Two apps now: the tunnel-wide one and a distinct new override app.
    assert len(ctx.fake.access_apps) == 2
    assert tunnel_app_id in ctx.fake.access_apps
    # The tunnel-wide app's policies must be untouched.
    assert ctx.fake.access_policies.get(tunnel_app_id, []) == policies_before
    override_apps = [a for a in ctx.fake.access_apps.values() if a["name"].startswith("override-")]
    assert len(override_apps) == 1


def test_remove_primary_service_does_not_delete_tunnel_wide_app() -> None:
    """Removing the primary ``system_interface`` service must NOT take down the
    tunnel-wide Access app. The tunnel-wide app's ``domain`` equals the primary
    hostname, so a by-domain fallback for the primary override would otherwise
    nuke the tunnel-wide app via ``_delete_override_app``.
    """
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "system_interface", "http://localhost:8080")
    assert len(ctx.fake.access_apps) == 1
    tunnel_app_id = next(iter(ctx.fake.access_apps.keys()))

    ctx.remove_service("alice--agent1", "alice", "system_interface")

    # Tunnel-wide app still exists; only its coverage list shrank.
    assert tunnel_app_id in ctx.fake.access_apps
    tunnel_app = ctx.fake.access_apps[tunnel_app_id]
    assert "system_interface--agent1--alice.example.com" not in tunnel_app["self_hosted_domains"]


def test_remove_service_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(ServiceNotFoundError):
        ctx.remove_service("alice--agent1", "alice", "nonexistent")


def test_tunnel_auth_get_set() -> None:
    ctx = make_fake_forwarding_ctx()
    # Before the tunnel exists, no tunnel-wide Access app exists, so the
    # policy read comes back as None (nothing to read from).
    assert ctx.get_tunnel_auth("alice--agent1") is None
    ctx.create_tunnel("alice", "agent1")
    # Just-created tunnel has no policies attached yet.
    fresh = ctx.get_tunnel_auth("alice--agent1")
    assert fresh is not None
    assert fresh.rules == []
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
    )
    assert isinstance(result, AdminAuth)
    assert result.username == "a1b2c3d4e5f67890"


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
        _authenticate_supertokens("valid-token", session_getter=lambda **kwargs: _SessionNoClaim())
    assert exc_info.value.status_code == 401
    assert "verified" in exc_info.value.detail


def test_authenticate_supertokens_raises_401_when_connection_uri_not_set() -> None:
    """When SUPERTOKENS_CONNECTION_URI is absent, raises 401."""
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "any-token",
            session_getter=lambda **kwargs: _FakeSession("ignored"),
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


def test_ctx_set_tunnel_auth_is_persisted_on_tunnel_wide_app() -> None:
    """set_tunnel_auth replaces the policies attached to the tunnel-wide
    Access app; the policy lives on Cloudflare now instead of in KV."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    tunnel_app = next(a for a in ctx.fake.access_apps.values() if a["name"] == "tunnel-alice--agent1")
    stored_policies = ctx.fake.access_policies.get(tunnel_app["id"], [])
    assert len(stored_policies) == 1
    assert stored_policies[0]["include"] == [{"email": {"email": "a@b.com"}}]


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
