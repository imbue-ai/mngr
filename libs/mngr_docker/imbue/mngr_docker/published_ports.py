from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr.providers.published_ports import choose_free_loopback_port
from imbue.mngr.providers.published_ports import guest_ports_in_declaration_order
from imbue.mngr.providers.published_ports import protocols_declared_for_guest_port
from imbue.mngr_docker.config import DockerProviderConfig
from imbue.mngr_docker.config import LOCAL_DOCKER_SSH_HOST
from imbue.mngr_docker.config import format_docker_publish_address
from imbue.mngr_docker.config import is_docker_daemon_local


@pure
def _publish_address_prefix(config: DockerProviderConfig) -> str:
    """The host-address part of a `docker run -p` spec, mirroring how sshd is published under `config`."""
    if config.ssh_bind_address is not None:
        return format_docker_publish_address(config.ssh_bind_address)
    if is_docker_daemon_local(config.host):
        return LOCAL_DOCKER_SSH_HOST
    return ""


@pure
def build_published_port_publish_specs(
    config: DockerProviderConfig,
    declarations: Sequence[PublishedPortDeclaration],
    host_port_by_guest_port: Mapping[int, int],
) -> list[str]:
    """The `docker run -p` specs that publish each declaration where sshd is published.

    A guest port in ``host_port_by_guest_port`` is pinned to that host port (so a TCP and a UDP
    declaration of one guest port share a number); any other gets a dynamic host port that is
    read back from the container's port bindings once it runs.
    """
    address_prefix = _publish_address_prefix(config)
    specs: list[str] = []
    for declaration in declarations:
        pinned_host_port = host_port_by_guest_port.get(declaration.guest_port)
        host_port_text = "" if pinned_host_port is None else str(pinned_host_port)
        host_part = f"{address_prefix}:{host_port_text}" if address_prefix else host_port_text
        specs.append(f"{host_part}:{declaration}")
    return specs


def choose_host_ports_for_shared_guest_ports(
    config: DockerProviderConfig, declarations: Sequence[PublishedPortDeclaration]
) -> dict[int, int]:
    """Pick one free host port for each guest port declared with more than one protocol.

    Docker allocates a dynamic host port per protocol independently, so a workload that
    advertises one number for TCP and UDP needs the port chosen up front. That is only
    possible for a local daemon, whose published ports land on this machine; a remote daemon's
    dual-protocol declarations keep Docker's independent dynamic ports.
    """
    if not is_docker_daemon_local(config.host):
        return {}
    host_port_by_guest_port: dict[int, int] = {}
    for guest_port in guest_ports_in_declaration_order(declarations):
        protocols = protocols_declared_for_guest_port(declarations, guest_port)
        if len(protocols) > 1:
            host_port_by_guest_port[guest_port] = choose_free_loopback_port(protocols)
    return host_port_by_guest_port


@pure
def read_published_endpoints_from_port_bindings(
    port_bindings: Mapping[str, Any],
    declarations: Sequence[PublishedPortDeclaration],
    host: str,
) -> list[PublishedEndpoint]:
    """Resolve each declaration against a container's live port bindings (the SDK's ``container.ports``).

    Raises MngrError when a declared port has no binding, which means the container was not
    run with the publish specs the declarations call for.
    """
    endpoints: list[PublishedEndpoint] = []
    for declaration in declarations:
        bindings = port_bindings.get(str(declaration))
        if not bindings:
            raise MngrError(f"Container has no port binding for published port {declaration}")
        endpoints.append(
            PublishedEndpoint(
                guest_port=declaration.guest_port,
                protocol=declaration.protocol,
                host=host,
                host_port=int(bindings[0]["HostPort"]),
            )
        )
    return endpoints
