"""The docker bridge of a remote outer host: where its latchkey gateway listens, and what keeps it there.

The bridge address is the one address a service on the outer host may bind so
that the agent's container reaches it while nothing off-host can (on a VPS,
the host itself has a public IP): the VPS-resident latchkey gateway listens
there. It is also what docker's
``host-gateway`` (``--add-host``) mapping resolves to inside the container.

Binding the address alone does not keep the gateway on the bridge, though.
Linux accepts a packet for any of its addresses on whichever interface it
arrives (the weak host model), so a packet for the bridge address that reaches
the VPS's public interface -- from a neighbour on the same segment, or a route
for the private range pointing at the VPS -- would be delivered to the bound
socket. The nftables policy the ``mngr-latchkey`` package ships
(:mod:`imbue.mngr_latchkey.remote.package`), loaded by its ``postinst`` and
at every boot by its systemd oneshot, closes that: traffic to the gateway's
port is dropped unless it arrives on the bridge interface itself or on
loopback (where the VPS's own processes, such as the compatibility reverse
tunnel, reach the gateway from).

The same policy confines the other direction. The bridge address is the
container's default gateway, so the container reaches every service the outer
host binds on all interfaces (its sshd, say), not only the one meant for it.
A new connection arriving on the bridge interface is therefore dropped unless
it is for the gateway's port; established traffic passes, so a
connection the host itself opened into the container keeps getting its
replies, and the container's own internet traffic is forwarded rather than
delivered to the host, so it is unaffected. The names the policy, its file and
its unit go by live here, beside the address resolution they belong with.
"""

from typing import Final

from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.remote._machine import MACHINE_GENERATION_FILE
from imbue.mngr_latchkey.remote._machine import is_machine_newer_outcome
from imbue.mngr_latchkey.remote._machine import machine_generation_check
from imbue.mngr_latchkey.remote.errors import MachineNewerError

# The interface of docker's default bridge network: the one every container the
# VPS provider creates is attached to, and whose IPv4 address is the container's
# default gateway (e.g. 172.17.0.1).
DOCKER_BRIDGE_INTERFACE_NAME: Final[str] = "docker0"

# The first command a provisioning pass sends, so it carries the check that
# stops the pass at a machine of a newer generation before anything is uploaded.
_RESOLVE_BRIDGE_ADDRESS_SCRIPT: Final[str] = "\n".join(
    (
        machine_generation_check(MACHINE_GENERATION_FILE, None),
        f"ip -4 -o addr show {DOCKER_BRIDGE_INTERFACE_NAME} 2>/dev/null | awk '{{print $4}}' | cut -d/ -f1 | head -n1",
    )
)

# Wildcard bind addresses a bridge-bound service must never be configured with.
_WILDCARD_LISTEN_HOSTS: Final[frozenset[str]] = frozenset({"", "0.0.0.0", "::", "[::]", "*"})

_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

# The nftables table the bridge-services policy lives in: its own, so it never
# collides with docker's tables or a distro firewall (ufw) and neither of those
# flushes it. The policy file lands in the package layout's nftables drop-in
# directory under this name.
BRIDGE_SERVICES_NFT_TABLE: Final[str] = "mngr_bridge_services"
BRIDGE_SERVICES_NFT_POLICY_FILENAME: Final[str] = "mngr-bridge-services.nft"

# The systemd oneshot that loads the policy at boot, ordered ahead of
# supervisord (which brings the gateway back) so the load runs before the
# gateway listens.
BRIDGE_SERVICES_FIREWALL_UNIT_NAME: Final[str] = "mngr-bridge-services-firewall"
NFT_BINARY_PATH: Final[str] = "/usr/sbin/nft"


class DockerBridgeAddressError(LatchkeyError, RuntimeError):
    """Raised when the docker bridge address of an outer host cannot be resolved."""


def resolve_docker_bridge_address(host: OuterHostInterface) -> str:
    """The docker bridge address on ``host``. Raises rather than yielding a wildcard.

    An unresolvable address fails closed: falling back to a wildcard bind on a
    VPS would put the service on the public interface, which no caller here
    wants. Callers wrap the error with the service they refuse to bind.

    Raises:
        MachineNewerError: when the host's software is of a newer generation
            than this build's, which is left alone.
        DockerBridgeAddressError: when the address cannot be resolved.
    """
    result = host.execute_idempotent_command(_RESOLVE_BRIDGE_ADDRESS_SCRIPT, timeout_seconds=_COMMAND_TIMEOUT_SECONDS)
    if result.success and is_machine_newer_outcome(result.stdout):
        raise MachineNewerError(host.get_name())
    bridge_address = result.stdout.strip()
    if not result.success or bridge_address in _WILDCARD_LISTEN_HOSTS:
        raise DockerBridgeAddressError(
            "Could not resolve a docker bridge address on host {} (got {!r}; stderr: {})".format(
                host.get_name(), bridge_address, result.stderr.strip() or "empty output"
            )
        )
    return bridge_address
