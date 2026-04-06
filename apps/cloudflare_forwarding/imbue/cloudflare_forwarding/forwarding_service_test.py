import pytest

from imbue.cloudflare_forwarding.errors import InvalidTunnelComponentError
from imbue.cloudflare_forwarding.errors import ServiceNotFoundError
from imbue.cloudflare_forwarding.errors import TunnelNotFoundError
from imbue.cloudflare_forwarding.errors import TunnelOwnershipError
from imbue.cloudflare_forwarding.forwarding_service import _extract_service_name_from_hostname
from imbue.cloudflare_forwarding.forwarding_service import make_hostname
from imbue.cloudflare_forwarding.forwarding_service import make_tunnel_name
from imbue.cloudflare_forwarding.primitives import AgentId
from imbue.cloudflare_forwarding.primitives import CloudflareTunnelId
from imbue.cloudflare_forwarding.primitives import DomainName
from imbue.cloudflare_forwarding.primitives import ServiceName
from imbue.cloudflare_forwarding.primitives import ServiceUrl
from imbue.cloudflare_forwarding.primitives import TunnelName
from imbue.cloudflare_forwarding.primitives import Username
from imbue.cloudflare_forwarding.testing import make_forwarding_service


def test_make_tunnel_name_format() -> None:
    assert make_tunnel_name(Username("alice"), AgentId("agent1")) == "alice--agent1"


def test_make_tunnel_name_allows_single_hyphen_in_agent_id() -> None:
    result = make_tunnel_name(Username("alice"), AgentId("agent-abc123"))
    assert result == "alice--agent-abc123"


def test_make_tunnel_name_rejects_double_hyphen_in_username() -> None:
    with pytest.raises(InvalidTunnelComponentError, match="Username"):
        make_tunnel_name(Username("alice--bob"), AgentId("agent1"))


def test_make_tunnel_name_rejects_double_hyphen_in_agent_id() -> None:
    with pytest.raises(InvalidTunnelComponentError, match="Agent ID"):
        make_tunnel_name(Username("alice"), AgentId("agent--1"))


def test_make_hostname_format() -> None:
    result = make_hostname(ServiceName("web"), AgentId("agent1"), Username("alice"), DomainName("example.com"))
    assert result == "web--agent1--alice.example.com"


def test_extract_service_name_from_hostname() -> None:
    result = _extract_service_name_from_hostname(
        "web--agent1--alice.example.com", AgentId("agent1"), Username("alice"), DomainName("example.com")
    )
    assert result == "web"


def test_extract_service_name_returns_none_for_non_matching() -> None:
    result = _extract_service_name_from_hostname(
        "other.example.com", AgentId("agent1"), Username("alice"), DomainName("example.com")
    )
    assert result is None


def test_create_tunnel_creates_new() -> None:
    svc, client = make_forwarding_service()
    info = svc.create_tunnel(Username("alice"), AgentId("agent1"))
    assert info.tunnel_name == "alice--agent1"
    assert info.tunnel_id == "tunnel-1"
    assert info.token == "token-for-tunnel-1"
    assert info.services == ()
    assert len(client.tunnels) == 1


def test_create_tunnel_reuses_existing() -> None:
    svc, client = make_forwarding_service()
    info1 = svc.create_tunnel(Username("alice"), AgentId("agent1"))
    info2 = svc.create_tunnel(Username("alice"), AgentId("agent1"))
    assert info1.tunnel_id == info2.tunnel_id
    assert len(client.tunnels) == 1


def test_create_tunnel_different_agents_create_different_tunnels() -> None:
    svc, client = make_forwarding_service()
    info1 = svc.create_tunnel(Username("alice"), AgentId("agent1"))
    info2 = svc.create_tunnel(Username("alice"), AgentId("agent2"))
    assert info1.tunnel_id != info2.tunnel_id
    assert len(client.tunnels) == 2


def test_list_tunnels_filters_by_user() -> None:
    svc, _ = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    svc.create_tunnel(Username("alice"), AgentId("agent2"))
    svc.create_tunnel(Username("bob"), AgentId("agent3"))

    tunnels = svc.list_tunnels(Username("alice"))
    assert len(tunnels) == 2
    names = {t.tunnel_name for t in tunnels}
    assert names == {"alice--agent1", "alice--agent2"}


def test_list_tunnels_empty_for_user_with_no_tunnels() -> None:
    svc, _ = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    tunnels = svc.list_tunnels(Username("bob"))
    assert tunnels == []


def test_delete_tunnel_removes_tunnel_and_dns() -> None:
    svc, client = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    svc.add_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("web"), ServiceUrl("http://localhost:8080"))

    assert len(client.dns_records) == 1
    svc.delete_tunnel(TunnelName("alice--agent1"), Username("alice"))
    assert len(client.tunnels) == 0
    assert len(client.dns_records) == 0


def test_delete_tunnel_raises_for_nonexistent() -> None:
    svc, _ = make_forwarding_service()
    with pytest.raises(TunnelNotFoundError):
        svc.delete_tunnel(TunnelName("alice--nonexistent"), Username("alice"))


def test_delete_tunnel_raises_for_wrong_owner() -> None:
    svc, _ = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    with pytest.raises(TunnelOwnershipError):
        svc.delete_tunnel(TunnelName("alice--agent1"), Username("bob"))


def test_add_service_creates_dns_and_ingress() -> None:
    svc, client = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    info = svc.add_service(
        TunnelName("alice--agent1"),
        Username("alice"),
        ServiceName("web"),
        ServiceUrl("http://localhost:8080"),
    )

    assert info.service_name == "web"
    assert info.hostname == "web--agent1--alice.example.com"
    assert info.service_url == "http://localhost:8080"
    assert len(client.dns_records) == 1
    assert client.dns_records[0]["name"] == "web--agent1--alice.example.com"

    config = client.get_tunnel_configuration(CloudflareTunnelId("tunnel-1"))
    ingress = config["config"]["ingress"]
    assert len(ingress) == 2
    assert ingress[0]["hostname"] == "web--agent1--alice.example.com"
    assert ingress[-1]["service"] == "http_status:404"


def test_add_multiple_services() -> None:
    svc, client = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    svc.add_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("web"), ServiceUrl("http://localhost:8080"))
    svc.add_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("api"), ServiceUrl("http://localhost:3000"))

    assert len(client.dns_records) == 2
    config = client.get_tunnel_configuration(CloudflareTunnelId("tunnel-1"))
    ingress = config["config"]["ingress"]
    assert len(ingress) == 3


def test_add_service_raises_for_wrong_owner() -> None:
    svc, _ = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    with pytest.raises(TunnelOwnershipError):
        svc.add_service(TunnelName("alice--agent1"), Username("bob"), ServiceName("web"), ServiceUrl("http://localhost:8080"))


def test_remove_service_removes_dns_and_ingress() -> None:
    svc, client = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    svc.add_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("web"), ServiceUrl("http://localhost:8080"))
    svc.add_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("api"), ServiceUrl("http://localhost:3000"))

    svc.remove_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("web"))

    assert len(client.dns_records) == 1
    assert client.dns_records[0]["name"] == "api--agent1--alice.example.com"
    config = client.get_tunnel_configuration(CloudflareTunnelId("tunnel-1"))
    ingress = config["config"]["ingress"]
    assert len(ingress) == 2
    assert ingress[0]["hostname"] == "api--agent1--alice.example.com"


def test_remove_service_raises_for_nonexistent() -> None:
    svc, _ = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    with pytest.raises(ServiceNotFoundError):
        svc.remove_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("nonexistent"))


def test_remove_service_raises_for_wrong_owner() -> None:
    svc, _ = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    svc.add_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("web"), ServiceUrl("http://localhost:8080"))
    with pytest.raises(TunnelOwnershipError):
        svc.remove_service(TunnelName("alice--agent1"), Username("bob"), ServiceName("web"))


def test_list_services_after_adding() -> None:
    svc, _ = make_forwarding_service()
    svc.create_tunnel(Username("alice"), AgentId("agent1"))
    svc.add_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("web"), ServiceUrl("http://localhost:8080"))
    svc.add_service(TunnelName("alice--agent1"), Username("alice"), ServiceName("api"), ServiceUrl("http://localhost:3000"))

    tunnels = svc.list_tunnels(Username("alice"))
    assert len(tunnels) == 1
    services = tunnels[0].services
    assert len(services) == 2
    names = {s.service_name for s in services}
    assert names == {"web", "api"}
