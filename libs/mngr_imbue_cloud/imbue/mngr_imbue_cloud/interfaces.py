from abc import ABC
from abc import abstractmethod
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel


class SliceReconcilerState(FrozenModel):
    """What the in-VM key reconciler currently looks like, as read from the slice VM."""

    is_unit_enabled: bool = Field(description="Whether the reconciler systemd unit is installed and enabled")
    desired_authorized_keys: str | None = Field(
        description="Content of the root-owned desired-state authorized_keys file, or None when absent"
    )
    is_live_matching_desired: bool = Field(
        description="Whether /root/.ssh/authorized_keys currently equals the desired-state file"
    )
    installed_content_hash: str | None = Field(
        description=(
            "sha256 of the installed reconciler unit + script contents, or None when either file is absent. "
            "Compared against what the current client version would install, so the heal pass replaces stale "
            "reconciler content, not just a missing/disabled unit."
        )
    )


class SliceVmAccessInterface(MutableModel, ABC):
    """Operations adoption performs on a leased slice, over its VM-root SSH endpoint.

    Every mutation runs as root inside the slice VM (container-side changes go
    through ``docker exec`` from the VM), so the whole surface needs exactly one
    working credential: the per-host client key on the VM-root sshd. Host-key
    probes are unauthenticated TCP handshakes against the slice's box-forwarded
    ports.
    """

    @abstractmethod
    def read_vm_root_authorized_keys(self) -> str | None:
        """Read the VM root's authorized_keys content, or None when the file is absent."""

    @abstractmethod
    def install_reconciler(self, desired_authorized_keys: str) -> None:
        """Install (or refresh) the in-VM key reconciler and run it once, asserting the desired state now."""

    @abstractmethod
    def read_reconciler_state(self) -> SliceReconcilerState:
        """Read the reconciler unit's enablement and desired-vs-live authorized_keys state."""

    @abstractmethod
    def install_vm_host_key(self, private_key_pem: str, public_key: str) -> None:
        """Install a new sshd host key on the VM (desired copy + live /etc/ssh) and reload its sshd."""

    @abstractmethod
    def install_container_host_key(self, private_key_pem: str, public_key: str) -> None:
        """Install a new sshd host key inside the workspace container and reload its sshd."""

    @abstractmethod
    def append_container_authorized_key(self, public_key: str) -> None:
        """Idempotently append a client public key to the container root's authorized_keys."""

    @abstractmethod
    def remove_container_authorized_key(self, public_key: str) -> None:
        """Remove a client public key line from the container root's authorized_keys."""

    @abstractmethod
    def is_endpoint_serving_host_key(self, port: int, public_key: str) -> bool:
        """Whether the sshd at the slice's address on ``port`` currently presents ``public_key``."""

    @abstractmethod
    def can_authenticate(self, port: int, private_key_path: Path) -> bool:
        """Whether an SSH connection to the slice's address on ``port`` authenticates with this key."""
