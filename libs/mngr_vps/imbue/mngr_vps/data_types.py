from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName


class AgentEndpoint(FrozenModel):
    """Where (and how) to SSH to the agent placed on a VPS.

    The realizer computes this from the VPS IP; the provider turns it into a
    pyinfra connector when building the agent ``Host``. For the container
    realizer the endpoint is the forwarded container sshd port reached with the
    container keypair; for the bare realizer it is the VPS's own port-22 sshd
    reached with the VPS keypair.
    """

    hostname: str = Field(description="Host to SSH to (the VPS public IP)")
    port: int = Field(description="SSH port the agent's sshd listens on")
    private_key_path: Path = Field(description="Client private key authenticating to the agent's sshd")
    known_hosts_path: Path = Field(description="known_hosts file pinning the agent sshd's host key")
    ssh_user: str | None = Field(
        default=None,
        description="SSH user, or None to use the connector default (the container realizer's default)",
    )


class RealizePlacementContext(FrozenModel):
    """Inputs a realizer needs to place an agent on an already-booted VPS.

    Mirrors the arguments the original ``_setup_container_on_vps`` took. The
    provider assembles this once the VPS is reachable; the realizer turns it
    into a running agent placement and returns a :class:`RealizedPlacement`.
    """

    host_id: HostId = Field(description="The host being created")
    name: HostName = Field(description="The host name")
    vps_ip: str = Field(description="Public IP of the booted VPS")
    base_image: str = Field(description="Base image to run/build the agent from (container realizer)")
    effective_start_args: tuple[str, ...] = Field(description="Runtime start args (e.g. docker run flags)")
    docker_build_args: tuple[str, ...] = Field(description="Build args; non-empty triggers an on-VPS image build")
    git_depth: int | None = Field(default=None, description="Shallow-clone depth for the local build context")
    tags: Mapping[str, str] | None = Field(default=None, description="User tags to stamp onto the placement")
    known_hosts: Sequence[str] | None = Field(default=None, description="Extra known_hosts entries for the agent")
    authorized_keys: Sequence[str] | None = Field(default=None, description="Extra authorized_keys for the agent")


class RealizedPlacement(FrozenModel):
    """What a realizer returns after placing an agent on a VPS.

    Carries the realizer-owned fields the provider copies into the host record.
    The container realizer fills these in; the bare realizer leaves them ``None``
    (there is no container or per-host docker volume).
    """

    container_name: str | None = Field(default=None, description="Agent container name on the VPS")
    container_id: str | None = Field(default=None, description="Agent container ID on the VPS")
    volume_name: str | None = Field(default=None, description="Per-host unified docker volume name")
    container_ssh_host_public_key: str | None = Field(
        default=None, description="The agent sshd's host public key (for the host record)"
    )
