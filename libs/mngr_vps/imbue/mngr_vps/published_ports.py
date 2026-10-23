from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import PublishedPortUnsupportedError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr.providers.published_ports import reject_sshd_port_declarations

# The agent's sshd inside the container, which the realizer publishes on the VM at ``container_ssh_port``.
CONTAINER_SSHD_GUEST_PORT: Final[int] = 22
# A published port is open to every source on the clouds mngr manages a firewall for: the
# listener behind it authenticates itself, and a viewer or a relay may come from any address.
EVERY_SOURCE_CIDR: Final[str] = "0.0.0.0/0"


@pure
def vps_publish_specs(declarations: Sequence[PublishedPortDeclaration]) -> list[str]:
    """The `docker run -p` specs for a VPS container: each guest port at the same number on every VM interface."""
    return [f"0.0.0.0:{declaration.guest_port}:{declaration}" for declaration in declarations]


@pure
def vps_published_endpoints(vps_ip: str, declarations: Sequence[PublishedPortDeclaration]) -> list[PublishedEndpoint]:
    """Where a VPS host's published ports are reachable: the VM's public address at the guest port number."""
    return [
        PublishedEndpoint(
            guest_port=declaration.guest_port,
            protocol=declaration.protocol,
            host=vps_ip,
            host_port=declaration.guest_port,
        )
        for declaration in declarations
    ]


def reject_declarations_outside_firewall(
    provider_name: ProviderInstanceName,
    declarations: Sequence[PublishedPortDeclaration],
    firewall_declarations: Sequence[PublishedPortDeclaration],
    prepare_command: str,
) -> None:
    """Refuse a declaration the provider's managed firewall was not prepared to let through.

    The firewall is opened once by the operator's ``prepare`` step from the provider config's
    ``firewall_published_ports``, so a create declaring any other port would produce a host
    whose published endpoint nothing can reach; raises PublishedPortUnsupportedError instead.
    """
    for declaration in declarations:
        if declaration not in firewall_declarations:
            raise PublishedPortUnsupportedError(
                provider_name,
                str(declaration),
                f"the provider's firewall does not allow it; add it to firewall_published_ports on the "
                f"[providers.{provider_name}] config block and run `{prepare_command}`",
            )


def reject_vm_port_collisions(
    provider_name: ProviderInstanceName,
    declarations: Sequence[PublishedPortDeclaration],
    container_ssh_port: int,
) -> None:
    """Refuse a declaration whose guest port the VM already uses; raises PublishedPortUnsupportedError.

    The container publishes each guest port on the VM at the same number, so guest port 22 would
    bind over the VM's own sshd and a guest port equal to ``container_ssh_port`` over the port the
    container's sshd is published on; either would fail ``docker run`` on the VM after it was
    ordered, so both are refused before any resource exists.
    """
    reject_sshd_port_declarations(provider_name, declarations, CONTAINER_SSHD_GUEST_PORT)
    for declaration in declarations:
        if declaration.guest_port == container_ssh_port:
            raise PublishedPortUnsupportedError(
                provider_name,
                str(declaration),
                f"VM port {container_ssh_port} is where the container's sshd is published (container_ssh_port)",
            )
