"""Resolve the docker bridge address of a remote outer host.

It is the one address a service on the outer host may bind so that the agent's
container reaches it while nothing off-host can (on a VPS, the host itself has
a public IP): the owner-exec vm daemon and the VPS-resident latchkey gateway
both listen there. It is also what docker's ``host-gateway`` (``--add-host``)
mapping resolves to inside the container.
"""

from typing import Final

from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_latchkey.core import LatchkeyError

# The IPv4 address of the ``docker0`` bridge interface (the agent container's
# default gateway, e.g. 172.17.0.1).
_RESOLVE_BRIDGE_ADDRESS_SCRIPT: Final[str] = (
    "ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1"
)

# Wildcard bind addresses a bridge-bound service must never be configured with.
_WILDCARD_LISTEN_HOSTS: Final[frozenset[str]] = frozenset({"", "0.0.0.0", "::", "[::]", "*"})

_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0


class DockerBridgeAddressError(LatchkeyError, RuntimeError):
    """Raised when the docker bridge address of an outer host cannot be resolved."""


def resolve_docker_bridge_address(host: OuterHostInterface) -> str:
    """The docker bridge address on ``host``. Raises rather than yielding a wildcard.

    An unresolvable address fails closed: falling back to a wildcard bind on a
    VPS would put the service on the public interface, which no caller here
    wants. Callers wrap the error with the service they refuse to bind.
    """
    result = host.execute_idempotent_command(_RESOLVE_BRIDGE_ADDRESS_SCRIPT, timeout_seconds=_COMMAND_TIMEOUT_SECONDS)
    bridge_address = result.stdout.strip()
    if not result.success or bridge_address in _WILDCARD_LISTEN_HOSTS:
        raise DockerBridgeAddressError(
            "Could not resolve a docker bridge address on host {} (got {!r}; stderr: {})".format(
                host.get_name(), bridge_address, result.stderr.strip() or "empty output"
            )
        )
    return bridge_address
