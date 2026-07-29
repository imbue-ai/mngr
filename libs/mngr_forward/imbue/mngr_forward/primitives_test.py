import pytest

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.primitives import FORWARD_SUBDOMAIN_PATTERN
from imbue.mngr_forward.primitives import ForwardPort
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.primitives import ServiceLabel
from imbue.mngr_forward.primitives import parse_forward_host


def test_forward_port_rejects_zero() -> None:
    with pytest.raises(ValueError):
        ForwardPort(0)


def test_forward_port_accepts_positive() -> None:
    assert ForwardPort(8421) == 8421


def test_session_cookie_name_constant() -> None:
    assert MNGR_FORWARD_SESSION_COOKIE_NAME == "mngr_forward_session"


@pytest.mark.parametrize(
    "host, expected",
    [
        ("agent-deadbeef.localhost", "agent-deadbeef"),
        ("agent-12ab34.localhost:8421", "agent-12ab34"),
        ("agent-AB.127.0.0.1", "agent-AB"),
        ("agent-ABCDEF.127.0.0.1:9000", "agent-ABCDEF"),
    ],
)
def test_subdomain_pattern_matches_valid_hosts(host: str, expected: str) -> None:
    match = FORWARD_SUBDOMAIN_PATTERN.match(host)
    assert match is not None
    assert match.group("agent").lower() == expected.lower()


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "example.com",
        "agent-XYZ.localhost",  # non-hex
        "wsagent-1234.localhost",  # missing prefix
        # Labels on a non-workspace host / labels with no agent coordinate.
        "svc.example.com",
        "svc.localhost",
        "",
    ],
)
def test_subdomain_pattern_rejects_invalid_hosts(host: str) -> None:
    assert FORWARD_SUBDOMAIN_PATTERN.match(host) is None


# --- parse_forward_host: the (agent, service) coordinates ------------------


@pytest.mark.parametrize(
    "host, agent, service_name, service_labels, workspace_domain",
    [
        ("agent-deadbeef.localhost", "agent-deadbeef", None, None, "agent-deadbeef.localhost"),
        ("agent-deadbeef.localhost:8421", "agent-deadbeef", None, None, "agent-deadbeef.localhost"),
        ("terminal.agent-12ab.localhost:8421", "agent-12ab", "terminal", "terminal", "agent-12ab.localhost"),
        # Deeper labels route to the same service: the LAST label before the
        # agent coordinate is the service; the rest is its sub-origin space.
        ("deep.svc.agent-12ab.localhost", "agent-12ab", "svc", "deep.svc", "agent-12ab.localhost"),
        ("a.b.c.svc.agent-12ab.localhost:9000", "agent-12ab", "svc", "a.b.c.svc", "agent-12ab.localhost"),
        # 127.0.0.1 stays a synonym.
        ("svc.agent-ab.127.0.0.1:9000", "agent-ab", "svc", "svc", "agent-ab.127.0.0.1"),
        # A service label that itself looks like an agent coordinate: the last
        # agent-<hex> label before the suffix wins as the agent.
        ("agent-ff.agent-12ab.localhost", "agent-12ab", "agent-ff", "agent-ff", "agent-12ab.localhost"),
        # DNS names are case-insensitive: labels are lowercased.
        ("TERMINAL.agent-12AB.LOCALHOST", "agent-12AB", "terminal", "terminal", "agent-12AB.localhost"),
    ],
)
def test_parse_forward_host_valid(
    host: str, agent: str, service_name: str | None, service_labels: str | None, workspace_domain: str
) -> None:
    parsed = parse_forward_host(host)
    assert parsed is not None
    assert str(parsed.agent_id_str) == agent
    assert parsed.service_name == service_name
    assert parsed.service_labels == service_labels
    assert str(parsed.workspace_domain) == workspace_domain


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:8421", "svc.localhost", "example.com", "svc.agent-xyz.localhost", ""],
)
def test_parse_forward_host_invalid(host: str) -> None:
    assert parse_forward_host(host) is None


# --- ServiceLabel -----------------------------------------------------------


@pytest.mark.parametrize("value", ["terminal", "openvscode", "my-app2", "a", "0x", "system_interface"])
def test_service_label_accepts_dns_safe_names(value: str) -> None:
    assert ServiceLabel(value) == value


@pytest.mark.parametrize("value", ["", "  ", "UPPER", "-lead", "trail-", "dot.name", "sp ace", "double--hyphen"])
def test_service_label_rejects_unsafe_names(value: str) -> None:
    with pytest.raises(ValueError):
        ServiceLabel(value)


def test_reverse_tunnel_spec_allows_zero_remote() -> None:
    spec = ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420))
    assert spec.remote_port == 0
    assert spec.local_port == 8420


def test_reverse_tunnel_spec_rejects_zero_local() -> None:
    with pytest.raises(ValueError):
        ReverseTunnelSpec(remote_port=NonNegativeInt(8420), local_port=0)  # ty: ignore[invalid-argument-type]
