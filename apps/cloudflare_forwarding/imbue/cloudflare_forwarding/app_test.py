import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient

import imbue.cloudflare_forwarding.app as app_mod
from imbue.cloudflare_forwarding.app import AuthPolicy
from imbue.cloudflare_forwarding.app import CloudflareApiError
from imbue.cloudflare_forwarding.app import InvalidTunnelComponentError
from imbue.cloudflare_forwarding.app import ServiceNotFoundError
from imbue.cloudflare_forwarding.app import TunnelNotFoundError
from imbue.cloudflare_forwarding.app import TunnelOwnershipError
from imbue.cloudflare_forwarding.app import cf_check
from imbue.cloudflare_forwarding.app import cf_list_all_pages
from imbue.cloudflare_forwarding.app import extract_service_name
from imbue.cloudflare_forwarding.app import extract_username_from_tunnel_name
from imbue.cloudflare_forwarding.app import make_hostname
from imbue.cloudflare_forwarding.app import make_tunnel_name
from imbue.cloudflare_forwarding.app import web_app
from imbue.cloudflare_forwarding.testing import make_fake_forwarding_ctx
from imbue.cloudflare_forwarding.testing import make_fake_tunnel_token


def _admin_headers(username: str = "testuser", password: str = "testsecret") -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _agent_headers(tunnel_id: str) -> dict[str, str]:
    token = make_fake_tunnel_token(tunnel_id)
    return {"Authorization": f"Bearer {token}"}


def _make_test_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a TestClient with the FastAPI app, injecting a fake context."""
    monkeypatch.setenv("USER_CREDENTIALS", json.dumps({"testuser": "testsecret"}))
    fake_ctx = make_fake_forwarding_ctx()
    monkeypatch.setattr(app_mod, "get_ctx", lambda: fake_ctx)
    return TestClient(web_app)


def test_make_tunnel_name_format() -> None:
    assert make_tunnel_name("alice", "agent1") == "alice--agent1"


def test_make_tunnel_name_allows_single_hyphen_in_agent_id() -> None:
    assert make_tunnel_name("alice", "agent-abc123") == "alice--agent-abc123"


def test_make_tunnel_name_rejects_double_hyphen_in_username() -> None:
    with pytest.raises(InvalidTunnelComponentError, match="Username"):
        make_tunnel_name("alice--bob", "agent1")


def test_make_tunnel_name_rejects_double_hyphen_in_agent_id() -> None:
    with pytest.raises(InvalidTunnelComponentError, match="Agent ID"):
        make_tunnel_name("alice", "agent--1")


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
            return httpx.Response(200, json={
                "success": True, "result": [{"id": "1"}, {"id": "2"}],
                "result_info": {"total_count": 3, "page": 1, "per_page": 2, "count": 2},
            })
        return httpx.Response(200, json={
            "success": True, "result": [{"id": "3"}],
            "result_info": {"total_count": 3, "page": 2, "per_page": 2, "count": 1},
        })

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


def test_route_bad_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels", headers=_admin_headers(password="wrong"))
    assert resp.status_code == 401


def test_route_malformed_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels/foo--bar/services", headers={"Authorization": "Bearer not-valid-base64!!!"})
    assert resp.status_code == 401
