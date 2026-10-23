from collections.abc import Sequence

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr.providers.published_ports import LOOPBACK_HOST
from imbue.mngr.providers.published_ports import choose_free_loopback_port
from imbue.mngr.providers.published_ports import guest_ports_in_declaration_order
from imbue.mngr.providers.published_ports import protocols_declared_for_guest_port
from imbue.mngr_lima.data_types import LimaPortForward


def choose_published_port_forwards(declarations: Sequence[PublishedPortDeclaration]) -> list[LimaPortForward]:
    """Pick a free loopback host port for each declared guest port, shared by every protocol it is declared with."""
    forwards: list[LimaPortForward] = []
    for guest_port in guest_ports_in_declaration_order(declarations):
        protocols = protocols_declared_for_guest_port(declarations, guest_port)
        host_port = choose_free_loopback_port(protocols)
        for protocol in protocols:
            forwards.append(LimaPortForward(guest_port=guest_port, host_port=host_port, protocol=protocol))
    return forwards


@pure
def published_endpoints_for_port_forwards(port_forwards: Sequence[LimaPortForward]) -> list[PublishedEndpoint]:
    return [
        PublishedEndpoint(
            guest_port=port_forward.guest_port,
            protocol=port_forward.protocol,
            host=LOOPBACK_HOST,
            host_port=port_forward.host_port,
        )
        for port_forward in port_forwards
    ]
