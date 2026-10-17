import socket

import pytest

from imbue.mngr.errors import PublishedPortUnsupportedError
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr.providers.published_ports import choose_free_loopback_port
from imbue.mngr.providers.published_ports import guest_ports_in_declaration_order
from imbue.mngr.providers.published_ports import identity_published_endpoints
from imbue.mngr.providers.published_ports import merge_published_endpoints
from imbue.mngr.providers.published_ports import protocols_declared_for_guest_port
from imbue.mngr.providers.published_ports import reject_sshd_port_declarations


def _declaration(guest_port: int, protocol: PortProtocol) -> PublishedPortDeclaration:
    return PublishedPortDeclaration(guest_port=guest_port, protocol=protocol)


def _endpoint(guest_port: int, protocol: PortProtocol, host_port: int) -> PublishedEndpoint:
    return PublishedEndpoint(guest_port=guest_port, protocol=protocol, host="127.0.0.1", host_port=host_port)


def test_identity_published_endpoints_use_the_guest_port_on_loopback() -> None:
    endpoints = identity_published_endpoints(
        [_declaration(59000, PortProtocol.UDP), _declaration(80, PortProtocol.TCP)]
    )
    assert endpoints == [_endpoint(59000, PortProtocol.UDP, 59000), _endpoint(80, PortProtocol.TCP, 80)]


def test_guest_ports_in_declaration_order_lists_each_port_once() -> None:
    declarations = [
        _declaration(59000, PortProtocol.UDP),
        _declaration(80, PortProtocol.TCP),
        _declaration(59000, PortProtocol.TCP),
    ]
    assert guest_ports_in_declaration_order(declarations) == [59000, 80]
    assert protocols_declared_for_guest_port(declarations, 59000) == [PortProtocol.UDP, PortProtocol.TCP]
    assert protocols_declared_for_guest_port(declarations, 443) == []


def test_merge_published_endpoints_replaces_matching_and_keeps_the_rest() -> None:
    existing = [_endpoint(59000, PortProtocol.UDP, 1), _endpoint(80, PortProtocol.TCP, 2)]
    updated = [_endpoint(59000, PortProtocol.UDP, 3)]
    assert merge_published_endpoints(existing, updated) == [
        _endpoint(80, PortProtocol.TCP, 2),
        _endpoint(59000, PortProtocol.UDP, 3),
    ]


def test_reject_sshd_port_declarations_refuses_the_sshd_guest_port_only() -> None:
    provider_name = ProviderInstanceName("docker")
    reject_sshd_port_declarations(provider_name, [_declaration(59000, PortProtocol.UDP)], sshd_guest_port=22)
    with pytest.raises(PublishedPortUnsupportedError, match="22/tcp.*sshd"):
        reject_sshd_port_declarations(provider_name, [_declaration(22, PortProtocol.TCP)], sshd_guest_port=22)


def test_choose_free_loopback_port_is_bindable_for_every_requested_protocol() -> None:
    port = choose_free_loopback_port([PortProtocol.TCP, PortProtocol.UDP])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_socket:
        tcp_socket.bind(("127.0.0.1", port))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
            udp_socket.bind(("127.0.0.1", port))


def test_choose_free_loopback_port_skips_a_port_whose_udp_side_is_taken() -> None:
    """A UDP listener on the kernel's next free TCP port must not be handed out as free for both."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy_udp_socket:
        busy_udp_socket.bind(("127.0.0.1", 0))
        busy_port = busy_udp_socket.getsockname()[1]
        chosen_ports = {choose_free_loopback_port([PortProtocol.UDP]) for _ in range(20)}
    assert busy_port not in chosen_ports
