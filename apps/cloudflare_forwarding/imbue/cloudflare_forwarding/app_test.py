import base64
import json

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request as StarletteRequest

from imbue.cloudflare_forwarding.app import CloudflareApiError
from imbue.cloudflare_forwarding.app import InvalidTunnelComponentError
from imbue.cloudflare_forwarding.app import ServiceNotFoundError
from imbue.cloudflare_forwarding.app import TunnelNotFoundError
from imbue.cloudflare_forwarding.app import TunnelOwnershipError
from imbue.cloudflare_forwarding.app import authenticate
from imbue.cloudflare_forwarding.app import cf_check
from imbue.cloudflare_forwarding.app import cf_list_all_pages
from imbue.cloudflare_forwarding.app import extract_service_name
from imbue.cloudflare_forwarding.app import make_hostname
from imbue.cloudflare_forwarding.app import make_tunnel_name
from imbue.cloudflare_forwarding.testing import make_fake_forwarding_ctx


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


def test_create_tunnel_creates_new() -> None:
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    assert info.tunnel_name == "alice--agent1"
    assert info.tunnel_id == "tunnel-1"
    assert info.token == "token-for-tunnel-1"
    assert info.services == []
    assert len(ctx.fake.tunnels) == 1


def test_create_tunnel_reuses_existing() -> None:
    ctx = make_fake_forwarding_ctx()
    info1 = ctx.create_tunnel("alice", "agent1")
    info2 = ctx.create_tunnel("alice", "agent1")
    assert info1.tunnel_id == info2.tunnel_id
    assert len(ctx.fake.tunnels) == 1


def test_create_tunnel_different_agents() -> None:
    ctx = make_fake_forwarding_ctx()
    info1 = ctx.create_tunnel("alice", "agent1")
    info2 = ctx.create_tunnel("alice", "agent2")
    assert info1.tunnel_id != info2.tunnel_id
    assert len(ctx.fake.tunnels) == 2


def test_list_tunnels_filters_by_user() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.create_tunnel("alice", "agent2")
    ctx.create_tunnel("bob", "agent3")

    tunnels = ctx.list_tunnels("alice")
    assert len(tunnels) == 2
    names = {t.tunnel_name for t in tunnels}
    assert names == {"alice--agent1", "alice--agent2"}


def test_list_tunnels_empty_for_user_with_no_tunnels() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    assert ctx.list_tunnels("bob") == []


def test_delete_tunnel_removes_tunnel_and_dns() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert len(ctx.fake.dns_records) == 1

    ctx.delete_tunnel("alice--agent1", "alice")
    assert len(ctx.fake.tunnels) == 0
    assert len(ctx.fake.dns_records) == 0


def test_delete_tunnel_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    with pytest.raises(TunnelNotFoundError):
        ctx.delete_tunnel("alice--nonexistent", "alice")


def test_delete_tunnel_raises_for_wrong_owner() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(TunnelOwnershipError):
        ctx.delete_tunnel("alice--agent1", "bob")


def test_add_service_creates_dns_and_ingress() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    info = ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")

    assert info.service_name == "web"
    assert info.hostname == "web--agent1--alice.example.com"
    assert info.service_url == "http://localhost:8080"
    assert len(ctx.fake.dns_records) == 1

    config = ctx.fake.get_tunnel_config("tunnel-1")
    ingress = config["config"]["ingress"]
    assert len(ingress) == 2
    assert ingress[0]["hostname"] == "web--agent1--alice.example.com"
    assert ingress[-1]["service"] == "http_status:404"


def test_add_multiple_services() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.add_service("alice--agent1", "alice", "api", "http://localhost:3000")

    assert len(ctx.fake.dns_records) == 2
    config = ctx.fake.get_tunnel_config("tunnel-1")
    assert len(config["config"]["ingress"]) == 3


def test_add_service_raises_for_wrong_owner() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(TunnelOwnershipError):
        ctx.add_service("alice--agent1", "bob", "web", "http://localhost:8080")


def test_remove_service_removes_dns_and_ingress() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.add_service("alice--agent1", "alice", "api", "http://localhost:3000")

    ctx.remove_service("alice--agent1", "alice", "web")

    assert len(ctx.fake.dns_records) == 1
    assert ctx.fake.dns_records[0]["name"] == "api--agent1--alice.example.com"
    config = ctx.fake.get_tunnel_config("tunnel-1")
    ingress = config["config"]["ingress"]
    assert len(ingress) == 2
    assert ingress[0]["hostname"] == "api--agent1--alice.example.com"


def test_remove_service_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(ServiceNotFoundError):
        ctx.remove_service("alice--agent1", "alice", "nonexistent")


def test_remove_service_raises_for_wrong_owner() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    with pytest.raises(TunnelOwnershipError):
        ctx.remove_service("alice--agent1", "bob", "web")


def test_list_services_after_adding() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.add_service("alice--agent1", "alice", "api", "http://localhost:3000")

    tunnels = ctx.list_tunnels("alice")
    assert len(tunnels) == 1
    assert len(tunnels[0].services) == 2
    names = {s.service_name for s in tunnels[0].services}
    assert names == {"web", "api"}


def test_authenticate_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER_CREDENTIALS", json.dumps({"alice": "secret123"}))
    encoded = base64.b64encode(b"alice:secret123").decode()
    scope = {"type": "http", "headers": [(b"authorization", f"Basic {encoded}".encode())]}
    req = StarletteRequest(scope)
    result = authenticate(req)
    assert result == "alice"


def test_authenticate_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER_CREDENTIALS", json.dumps({"alice": "secret123"}))
    encoded = base64.b64encode(b"alice:wrong").decode()
    scope = {"type": "http", "headers": [(b"authorization", f"Basic {encoded}".encode())]}
    req = StarletteRequest(scope)
    with pytest.raises(HTTPException, match="Invalid credentials"):
        authenticate(req)


def test_authenticate_malformed_base64() -> None:
    scope = {"type": "http", "headers": [(b"authorization", b"Basic !!!not-valid-base64!!!")]}
    req = StarletteRequest(scope)
    with pytest.raises(HTTPException, match="Malformed credentials"):
        authenticate(req)
