"""The docker bridge of a remote outer host: where its bridge-bound services listen, and what keeps them there.

The bridge address is the one address a service on the outer host may bind so
that the agent's container reaches it while nothing off-host can (on a VPS,
the host itself has a public IP): the owner-exec vm daemon and the
VPS-resident latchkey gateway both listen there. It is also what docker's
``host-gateway`` (``--add-host``) mapping resolves to inside the container.

Binding the address alone does not keep the services on the bridge, though.
Linux accepts a packet for any of its addresses on whichever interface it
arrives (the weak host model), so a packet for the bridge address that reaches
the VPS's public interface -- from a neighbour on the same segment, or a route
for the private range pointing at the VPS -- would be delivered to the bound
socket. The nftables policy here closes that: traffic to the services' ports
is dropped unless it arrives on the bridge interface itself or on loopback
(where the VPS's own processes, such as the compatibility reverse tunnel,
reach the gateway from).

The same policy confines the other direction. The bridge address is the
container's default gateway, so the container reaches every service the outer
host binds on all interfaces (its sshd, say), not only the two meant for it.
A new connection arriving on the bridge interface is therefore dropped unless
it is for one of the services' ports; established traffic passes, so a
connection the host itself opened into the container keeps getting its
replies, and the container's own internet traffic is forwarded rather than
delivered to the host, so it is unaffected.
"""

import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_latchkey.core import LatchkeyError

# The interface of docker's default bridge network: the one every container the
# VPS provider creates is attached to, and whose IPv4 address is the container's
# default gateway (e.g. 172.17.0.1).
DOCKER_BRIDGE_INTERFACE_NAME: Final[str] = "docker0"

_RESOLVE_BRIDGE_ADDRESS_SCRIPT: Final[str] = (
    f"ip -4 -o addr show {DOCKER_BRIDGE_INTERFACE_NAME} 2>/dev/null | awk '{{print $4}}' | cut -d/ -f1 | head -n1"
)

# Wildcard bind addresses a bridge-bound service must never be configured with.
_WILDCARD_LISTEN_HOSTS: Final[frozenset[str]] = frozenset({"", "0.0.0.0", "::", "[::]", "*"})

_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

# ``apt-get update`` + a package install on a cold VPS can run into the minutes.
_INSTALL_TIMEOUT_SECONDS: Final[float] = 300.0

# The nftables table the bridge-services policy lives in: its own, so it never
# collides with docker's tables or a distro firewall (ufw) and neither of those
# flushes it.
BRIDGE_SERVICES_NFT_TABLE: Final[str] = "mngr_bridge_services"
BRIDGE_SERVICES_NFT_POLICY_PATH: Final[Path] = Path("/etc/nftables.d/mngr-bridge-services.nft")

# The systemd oneshot that loads the policy at boot, ordered ahead of the
# owner-exec daemon (which systemd brings back after a reboot) so the load runs
# before the daemon listens. The gateway's tmpfs secrets do not survive a
# reboot, so the gateway itself stays down until the next provisioning pass,
# which re-applies the policy anyway.
BRIDGE_SERVICES_FIREWALL_UNIT_NAME: Final[str] = "mngr-bridge-services-firewall"
_BRIDGE_SERVICES_FIREWALL_UNIT_PATH: Final[Path] = Path(
    f"/etc/systemd/system/{BRIDGE_SERVICES_FIREWALL_UNIT_NAME}.service"
)
_NFT_BINARY_PATH: Final[str] = "/usr/sbin/nft"

_ENSURE_NFTABLES_INSTALLED_SCRIPT: Final[str] = "\n".join(
    (
        "set -e",
        "export DEBIAN_FRONTEND=noninteractive",
        f"if [ ! -x {_NFT_BINARY_PATH} ]; then",
        "  apt-get update",
        "  apt-get install -y nftables",
        "fi",
    )
)


class DockerBridgeAddressError(LatchkeyError, RuntimeError):
    """Raised when the docker bridge address of an outer host cannot be resolved."""


class DockerBridgeFirewallError(LatchkeyError, RuntimeError):
    """Raised when the policy keeping bridge-bound services on the docker bridge cannot be applied."""


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


@pure
def build_bridge_services_nftables_policy(ports: Sequence[int]) -> str:
    """The nftables policy confining ``ports`` to the bridge and loopback, and the bridge to ``ports``.

    Its own table, with the add-then-delete-then-add pattern so re-loading the
    file converges. The chain accepts by default, so traffic on every other
    interface (the VPS's sshd on its public interface, the desktop's tunnels on
    loopback) is untouched: only the listed ports are dropped when they arrive
    off the bridge, and only new connections are dropped when they arrive on
    the bridge for any other port.
    """
    port_set = ", ".join(str(port) for port in ports)
    return f"""\
#!{_NFT_BINARY_PATH} -f
# Managed by mngr (mngr_latchkey remote provisioning).
# The services the VPS binds on its docker bridge address (the latchkey gateway
# and the owner-exec daemon) answer only the agent's container and the VPS's
# own processes: a packet for their ports arriving on any other interface (the
# public one, a user-defined docker network) drops here, before the bound
# socket ever receives it. And the container reaches only those services: a new
# connection arriving from the bridge for any other port on this host drops too.
add table inet {BRIDGE_SERVICES_NFT_TABLE}
delete table inet {BRIDGE_SERVICES_NFT_TABLE}
add table inet {BRIDGE_SERVICES_NFT_TABLE} {{
    chain input {{
        type filter hook input priority filter; policy accept;
        iifname != "{DOCKER_BRIDGE_INTERFACE_NAME}" iifname != "lo" tcp dport {{ {port_set} }} counter drop
        iifname "{DOCKER_BRIDGE_INTERFACE_NAME}" tcp dport {{ {port_set} }} accept
        iifname "{DOCKER_BRIDGE_INTERFACE_NAME}" ct state new counter drop
    }}
}}
"""


@pure
def _build_bridge_services_firewall_unit() -> str:
    """A oneshot unit that loads the policy at boot, ordered before the services it protects come up.

    ``After=nftables.service`` keeps a distro policy that starts by flushing the
    ruleset from wiping this one; the ordering is a no-op on a VPS that does not
    run that service.
    """
    return "\n".join(
        (
            "[Unit]",
            "Description=Keep mngr's bridge-bound services (latchkey gateway, owner-exec) on the docker bridge",
            "After=nftables.service",
            "Before=docker.service owner-exec-vm.service supervisor.service",
            "",
            "[Service]",
            "Type=oneshot",
            "RemainAfterExit=yes",
            f"ExecStart={_NFT_BINARY_PATH} -f {BRIDGE_SERVICES_NFT_POLICY_PATH}",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )


def ensure_bridge_services_firewalled(host: OuterHostInterface, ports: Sequence[int]) -> None:
    """Install and apply the policy confining ``ports`` to the docker bridge and loopback, and the bridge to ``ports``.

    Idempotent: nftables is installed only when missing, and applying the policy
    again converges. For a genuinely remote outer host only -- the caller keeps
    the user's own machine out of this. Raises :class:`DockerBridgeFirewallError`
    on any failure, before the services it protects are started.
    """
    host_name = host.get_name()
    with log_span("Ensuring nftables is installed on host {}", host_name):
        install_result = host.execute_idempotent_command(
            _ENSURE_NFTABLES_INSTALLED_SCRIPT, timeout_seconds=_INSTALL_TIMEOUT_SECONDS
        )
    if not install_result.success:
        raise DockerBridgeFirewallError(
            "Failed to install nftables on host {}: {}".format(
                host_name, install_result.stderr.strip() or install_result.stdout.strip()
            )
        )

    host.write_file(
        BRIDGE_SERVICES_NFT_POLICY_PATH,
        build_bridge_services_nftables_policy(ports).encode("utf-8"),
        mode="0644",
        is_atomic=True,
    )
    host.write_file(
        _BRIDGE_SERVICES_FIREWALL_UNIT_PATH,
        _build_bridge_services_firewall_unit().encode("utf-8"),
        mode="0644",
        is_atomic=True,
    )
    # ``restart`` re-runs the oneshot so a changed policy is applied now, not
    # only at the next boot.
    unit = shlex.quote(BRIDGE_SERVICES_FIREWALL_UNIT_NAME)
    with log_span("Applying the docker-bridge firewall for ports {} on host {}", list(ports), host_name):
        apply_result = host.execute_idempotent_command(
            f"systemctl daemon-reload && systemctl enable --now {unit} && systemctl restart {unit}",
            timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
        )
    if not apply_result.success:
        raise DockerBridgeFirewallError(
            "Failed to apply the docker-bridge firewall on host {}: {}".format(
                host_name, apply_result.stderr.strip() or apply_result.stdout.strip()
            )
        )
