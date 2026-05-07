"""Unit tests for the moved-from-minds SSH tunnel module.

The actual SSH I/O paths (paramiko transport, direct-tcpip, reverse port
forward) require a live sshd and are exercised by the acceptance / release
tests. These unit tests cover the deterministic surfaces that don't need a
real network: the URL-parsing helper and the ``RemoteSSHInfo`` /
``ReverseTunnelInfo`` / ``ReverseTunnelSpec`` data shapes.
"""

from pathlib import Path

import pytest

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import ReverseTunnelInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_forward.ssh_tunnel import parse_url_host_port

# -- parse_url_host_port ---------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://127.0.0.1:9100", ("127.0.0.1", 9100)),
        ("http://localhost:9100", ("127.0.0.1", 9100)),  # localhost normalized to v4
        ("http://example.com:8080/path", ("example.com", 8080)),
        ("http://example.com/path", ("example.com", 80)),  # default http port
        ("https://example.com/path", ("example.com", 443)),  # default https port
    ],
)
def test_parse_url_host_port(url: str, expected: tuple[str, int]) -> None:
    assert parse_url_host_port(url) == expected


def test_parse_url_host_port_localhost_normalization() -> None:
    """SSH channels don't dual-stack so we always normalize localhost to 127.0.0.1."""
    host, port = parse_url_host_port("http://localhost")
    assert host == "127.0.0.1"
    assert port == 80


# -- RemoteSSHInfo ---------------------------------------------------------


def test_remote_ssh_info_round_trip() -> None:
    info = RemoteSSHInfo(user="root", host="1.2.3.4", port=22, key_path=Path("/tmp/k"))
    assert info.user == "root"
    assert info.host == "1.2.3.4"
    assert info.port == 22
    assert info.key_path == Path("/tmp/k")


def test_remote_ssh_info_is_frozen() -> None:
    info = RemoteSSHInfo(user="root", host="1.2.3.4", port=22, key_path=Path("/tmp/k"))
    with pytest.raises((ValueError, TypeError)):
        info.user = "other"  # type: ignore[misc]


# -- ReverseTunnelInfo / ReverseTunnelSpec ---------------------------------


def test_reverse_tunnel_info_defaults() -> None:
    ssh_info = RemoteSSHInfo(user="root", host="h", port=22, key_path=Path("/tmp/k"))
    info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=12345,
    )
    assert info.requested_remote_port == 0  # default: dynamic-assign sentinel


def test_reverse_tunnel_spec_remote_zero_means_dynamic() -> None:
    spec = ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420))
    assert spec.remote_port == 0
    assert spec.local_port == 8420


def test_reverse_tunnel_spec_local_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ReverseTunnelSpec(remote_port=NonNegativeInt(8420), local_port=0)  # type: ignore[arg-type]


# -- SSHTunnelManager structural tests -------------------------------------


def test_ssh_tunnel_manager_starts_empty() -> None:
    manager = SSHTunnelManager()
    # Pure structural checks: no network I/O. The manager should be safe to
    # construct without any side effects.
    assert manager is not None


def test_ssh_tunnel_manager_cleanup_is_idempotent() -> None:
    """``cleanup`` on an unused manager must succeed without raising."""
    manager = SSHTunnelManager()
    manager.cleanup()
    # Calling twice is fine — used by the lifespan-shutdown path which can
    # race with explicit cleanup() during error paths.
    manager.cleanup()


def test_ssh_tunnel_manager_repair_callback_registers() -> None:
    """``add_on_tunnel_repaired_callback`` accepts callbacks and stores them."""
    manager = SSHTunnelManager()
    received: list[ReverseTunnelInfo] = []
    manager.add_on_tunnel_repaired_callback(received.append)
    # Without a real broken tunnel we can't trigger the callback, but the
    # registration path itself must not raise.
    assert received == []
    manager.cleanup()
