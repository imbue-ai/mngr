import socket
from collections.abc import Sequence
from typing import Final

from loguru import logger

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import PublishedPortUnsupportedError
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration

LOOPBACK_HOST: Final[str] = "127.0.0.1"
_FREE_PORT_ATTEMPT_COUNT: Final[int] = 32


class NoFreeHostPortError(MngrError):
    """Raised when no loopback port free for every requested protocol could be found."""


@pure
def identity_published_endpoints(declarations: Sequence[PublishedPortDeclaration]) -> list[PublishedEndpoint]:
    """The endpoints of a host whose workload binds the declared ports on this machine itself (the local provider)."""
    return [
        PublishedEndpoint(
            guest_port=declaration.guest_port,
            protocol=declaration.protocol,
            host=LOOPBACK_HOST,
            host_port=declaration.guest_port,
        )
        for declaration in declarations
    ]


@pure
def guest_ports_in_declaration_order(declarations: Sequence[PublishedPortDeclaration]) -> list[int]:
    """Each declared guest port once, in the order it was first declared."""
    guest_ports: list[int] = []
    for declaration in declarations:
        if declaration.guest_port not in guest_ports:
            guest_ports.append(declaration.guest_port)
    return guest_ports


@pure
def protocols_declared_for_guest_port(
    declarations: Sequence[PublishedPortDeclaration], guest_port: int
) -> list[PortProtocol]:
    return [declaration.protocol for declaration in declarations if declaration.guest_port == guest_port]


@pure
def merge_published_endpoints(
    existing: Sequence[PublishedEndpoint], updated: Sequence[PublishedEndpoint]
) -> list[PublishedEndpoint]:
    """Replace each existing endpoint for a (guest port, protocol) that ``updated`` also answers; keep the rest."""
    updated_keys = {(endpoint.guest_port, endpoint.protocol) for endpoint in updated}
    kept = [endpoint for endpoint in existing if (endpoint.guest_port, endpoint.protocol) not in updated_keys]
    return kept + list(updated)


def reject_sshd_port_declarations(
    provider_name: ProviderInstanceName,
    declarations: Sequence[PublishedPortDeclaration],
    sshd_guest_port: int,
) -> None:
    """Refuse a declaration of the guest port the provider already publishes sshd on; raises PublishedPortUnsupportedError."""
    for declaration in declarations:
        if declaration.guest_port == sshd_guest_port:
            raise PublishedPortUnsupportedError(
                provider_name,
                str(declaration),
                f"guest port {sshd_guest_port} is the host's sshd, which the provider publishes itself",
            )


def record_published_endpoints_on_host(host: HostInterface, endpoints: Sequence[PublishedEndpoint]) -> None:
    """Write the host's published endpoints into its certified data, replacing those recorded before."""
    certified_data = host.get_certified_data()
    updated_data = certified_data.model_copy_update(
        to_update(certified_data.field_ref().published_endpoints, tuple(endpoints)),
    )
    host.set_certified_data(updated_data)


def choose_free_loopback_port(protocols: Sequence[PortProtocol]) -> int:
    """Pick a loopback port that is free for every protocol in ``protocols``.

    The kernel hands out a free TCP port, which is then also checked as a UDP bind when UDP is
    among the protocols, so a guest port declared for both protocols gets one host port number.
    Raises NoFreeHostPortError when no such port turns up after a bounded number of attempts.
    """
    is_udp_needed = PortProtocol.UDP in protocols
    for _ in range(_FREE_PORT_ATTEMPT_COUNT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_socket:
            tcp_socket.bind((LOOPBACK_HOST, 0))
            candidate = int(tcp_socket.getsockname()[1])
            if not is_udp_needed:
                return candidate
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
                try:
                    udp_socket.bind((LOOPBACK_HOST, candidate))
                except OSError as e:
                    logger.trace("Loopback port {} is free for TCP but not UDP ({}); trying another", candidate, e)
                    continue
                return candidate
    raise NoFreeHostPortError(
        f"Could not find a loopback port free for {', '.join(protocol.value.lower() for protocol in protocols)} "
        f"after {_FREE_PORT_ATTEMPT_COUNT} attempts"
    )
