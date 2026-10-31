from collections.abc import Mapping
from collections.abc import Sequence

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import PublishedPortUnsupportedError
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.modal_proxy.data_types import TunnelInfo


def reject_udp_declarations(
    provider_name: ProviderInstanceName, declarations: Sequence[PublishedPortDeclaration]
) -> None:
    """Modal tunnels carry TCP only, so a UDP declaration fails the create before any sandbox exists (D3)."""
    for declaration in declarations:
        if declaration.protocol == PortProtocol.UDP:
            raise PublishedPortUnsupportedError(
                provider_name, str(declaration), "Modal sandbox tunnels carry TCP only, so UDP cannot be published"
            )


@pure
def sandbox_unencrypted_ports(sshd_port: int, declarations: Sequence[PublishedPortDeclaration]) -> list[int]:
    """The ports a sandbox is created with tunnels for: sshd first, then each declared guest port once."""
    ports = [sshd_port]
    for declaration in declarations:
        if declaration.guest_port not in ports:
            ports.append(declaration.guest_port)
    return ports


@pure
def published_endpoints_from_tunnels(
    tunnels: Mapping[int, TunnelInfo], declarations: Sequence[PublishedPortDeclaration]
) -> list[PublishedEndpoint]:
    """Resolve each declaration against the sandbox's tunnels; raises MngrError when a declared port has none."""
    endpoints: list[PublishedEndpoint] = []
    for declaration in declarations:
        tunnel = tunnels.get(declaration.guest_port)
        if tunnel is None:
            raise MngrError(f"Modal sandbox has no tunnel for published port {declaration}")
        tunnel_host, tunnel_port = tunnel.tcp_socket
        endpoints.append(
            PublishedEndpoint(
                guest_port=declaration.guest_port,
                protocol=declaration.protocol,
                host=tunnel_host,
                host_port=tunnel_port,
            )
        )
    return endpoints
