from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr

# SSH key-file names under a provider instance's ``key_dir``. Shared so the
# provider (substrate) and the bare realizer -- which reuses the VPS keys to
# reach the agent on the VM's own port-22 sshd -- refer to the same files.
VPS_SSH_KEY_NAME: Final[str] = "vps_ssh_key"
VPS_HOST_KEY_NAME: Final[str] = "host_key"
VPS_KNOWN_HOSTS_NAME: Final[str] = "vps_known_hosts"


class VpsInstanceId(NonEmptyStr):
    """Unique identifier for a VPS instance as assigned by the provider."""


class IsolationMode(UpperCaseStrEnum):
    """How the agent is isolated on its VPS -- the realization axis of a provider.

    Selects the ``HostRealizer`` the provider uses to place an agent on a booted
    VPS. ``CONTAINER`` (the default) runs the agent inside a Docker container;
    ``NONE`` runs it directly on the VPS OS (no container). Leaves room for a
    future sandboxed level (e.g. gVisor) that folds today's ``docker_runtime``
    knob into this enum.
    """

    CONTAINER = auto()
    NONE = auto()


class VpsInstanceStatus(UpperCaseStrEnum):
    """Status of a VPS instance as reported by the provider API."""

    PENDING = auto()
    ACTIVE = auto()
    HALTED = auto()
    DESTROYING = auto()
    UNKNOWN = auto()
