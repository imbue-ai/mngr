import pytest

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.primitives import FORWARD_SUBDOMAIN_PATTERN
from imbue.mngr_forward.primitives import ForwardPort
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.primitives import ReverseTunnelSpec


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
    assert match.group(1).lower() == expected.lower()


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "example.com",
        "agent-XYZ.localhost",  # non-hex
        "wsagent-1234.localhost",  # missing prefix
        "",
    ],
)
def test_subdomain_pattern_rejects_invalid_hosts(host: str) -> None:
    assert FORWARD_SUBDOMAIN_PATTERN.match(host) is None


def test_reverse_tunnel_spec_allows_zero_remote() -> None:
    spec = ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420))
    assert spec.remote_port == 0
    assert spec.local_port == 8420


def test_reverse_tunnel_spec_rejects_zero_local() -> None:
    with pytest.raises(ValueError):
        ReverseTunnelSpec(remote_port=NonNegativeInt(8420), local_port=0)  # type: ignore[arg-type]
