import pytest

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.primitives import FORWARD_SUBDOMAIN_PATTERN
from imbue.mngr_forward.primitives import ForwardPort
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.primitives import ServiceLabel
from imbue.mngr_forward.primitives import parse_forward_host

# Host-header host ids require the full 32 hex characters (matching HostId).
_HOST_A = "host-" + "0123456789abcdef0123456789abcdef"
_HOST_B = "host-" + "feedfacefeedfacefeedfacefeedface"


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
        (f"{_HOST_A}.localhost", _HOST_A),
        (f"{_HOST_A}.localhost:8421", _HOST_A),
        (f"{_HOST_B}.127.0.0.1", _HOST_B),
        (f"{_HOST_B.upper().replace('HOST', 'host')}.127.0.0.1:9000", _HOST_B),
    ],
)
def test_subdomain_pattern_matches_valid_hosts(host: str, expected: str) -> None:
    match = FORWARD_SUBDOMAIN_PATTERN.match(host)
    assert match is not None
    assert match.group("host").lower() == expected.lower()


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "example.com",
        # Non-hex id, too-short id (host ids are 32 hex chars), mangled prefix.
        "host-XYZ.localhost",
        "host-1234.localhost",
        f"ws{_HOST_A}.localhost",
        # Labels on a non-workspace host / labels with no host coordinate.
        "svc.example.com",
        "svc.localhost",
        "",
    ],
)
def test_subdomain_pattern_rejects_invalid_hosts(host: str) -> None:
    assert FORWARD_SUBDOMAIN_PATTERN.match(host) is None


# --- parse_forward_host: the (workspace, service) coordinates ---------------


@pytest.mark.parametrize(
    "host, host_id, service_name, service_labels, workspace_domain",
    [
        (f"{_HOST_A}.localhost", _HOST_A, None, None, f"{_HOST_A}.localhost"),
        (f"{_HOST_A}.localhost:8421", _HOST_A, None, None, f"{_HOST_A}.localhost"),
        (f"terminal.{_HOST_A}.localhost:8421", _HOST_A, "terminal", "terminal", f"{_HOST_A}.localhost"),
        # Deeper labels route to the same service: the LAST label before the
        # host coordinate is the service; the rest is its sub-origin space.
        (f"deep.svc.{_HOST_A}.localhost", _HOST_A, "svc", "deep.svc", f"{_HOST_A}.localhost"),
        (f"a.b.c.svc.{_HOST_A}.localhost:9000", _HOST_A, "svc", "a.b.c.svc", f"{_HOST_A}.localhost"),
        # 127.0.0.1 stays a synonym.
        (f"svc.{_HOST_B}.127.0.0.1:9000", _HOST_B, "svc", "svc", f"{_HOST_B}.127.0.0.1"),
        # DNS names are case-insensitive: labels are lowercased.
        (f"TERMINAL.{_HOST_A}.LOCALHOST", _HOST_A, "terminal", "terminal", f"{_HOST_A}.localhost"),
        # The shell's legacy underscore name works as a label too.
        (
            f"system_interface.{_HOST_A}.localhost",
            _HOST_A,
            "system_interface",
            "system_interface",
            f"{_HOST_A}.localhost",
        ),
    ],
)
def test_parse_forward_host_valid(
    host: str, host_id: str, service_name: str | None, service_labels: str | None, workspace_domain: str
) -> None:
    parsed = parse_forward_host(host)
    assert parsed is not None
    assert str(parsed.host_id_str) == host_id
    assert parsed.service_name == service_name
    assert parsed.service_labels == service_labels
    assert str(parsed.workspace_domain) == workspace_domain


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:8421", "svc.localhost", "example.com", "svc.host-xyz.localhost", ""],
)
def test_parse_forward_host_invalid(host: str) -> None:
    assert parse_forward_host(host) is None


# --- ServiceLabel -----------------------------------------------------------


@pytest.mark.parametrize("value", ["terminal", "openvscode", "my-app2", "a", "0x", "system_interface"])
def test_service_label_accepts_hostname_safe_names(value: str) -> None:
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
