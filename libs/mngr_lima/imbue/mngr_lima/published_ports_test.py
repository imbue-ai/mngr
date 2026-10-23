import socket

from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr_lima.data_types import LimaPortForward
from imbue.mngr_lima.published_ports import choose_published_port_forwards
from imbue.mngr_lima.published_ports import published_endpoints_for_port_forwards


def test_choose_published_port_forwards_shares_one_host_port_across_a_guest_ports_protocols() -> None:
    forwards = choose_published_port_forwards(
        [
            PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.UDP),
            PublishedPortDeclaration(guest_port=8080, protocol=PortProtocol.TCP),
            PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.TCP),
        ]
    )

    assert [(forward.guest_port, forward.protocol) for forward in forwards] == [
        (59000, PortProtocol.UDP),
        (59000, PortProtocol.TCP),
        (8080, PortProtocol.TCP),
    ]
    assert forwards[0].host_port == forwards[1].host_port
    assert forwards[2].host_port != forwards[0].host_port
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.bind(("127.0.0.1", forwards[0].host_port))


def test_published_endpoints_for_port_forwards_are_the_host_ports_on_loopback() -> None:
    forwards = [LimaPortForward(guest_port=59000, host_port=40123, protocol=PortProtocol.UDP)]
    assert published_endpoints_for_port_forwards(forwards) == [
        PublishedEndpoint(guest_port=59000, protocol=PortProtocol.UDP, host="127.0.0.1", host_port=40123)
    ]
