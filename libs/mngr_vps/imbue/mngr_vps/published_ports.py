from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import PublishedPortUnsupportedError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration

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
