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


# Instance-level identity marker recording a host's *placement* (container vs
# bare), stamped at create and readable from the cloud API WITHOUT SSH (an EC2/
# Azure tag, a GCE metadata item). This lets discovery pick the realizer matching
# a host's actual placement before opening any on-host store -- so a bare host is
# found by a default-CONTAINER config, and vice versa. The value is the lowercased
# ``IsolationMode`` name (``"none"`` / ``"container"``), per the conventional
# lowercase tag/label charset.
ISOLATION_TAG_KEY: Final[str] = "mngr-isolation"


def isolation_marker_value(isolation: IsolationMode) -> str:
    """The ``mngr-isolation`` tag/metadata value to stamp for a placement (lowercased)."""
    return isolation.value.lower()


def isolation_from_marker(marker_value: str | None) -> IsolationMode:
    """Resolve a host's placement from its instance ``mngr-isolation`` marker.

    Hosts created before the marker existed carry no value (``None``); they were
    all CONTAINER placements, so an absent marker defaults to ``CONTAINER`` to
    preserve the prior behavior. A *present* value is parsed strictly: an
    unrecognized marker raises rather than being silently mis-resolved (the marker
    is mngr-written, so a bad value is corruption worth surfacing).
    """
    if marker_value is None:
        return IsolationMode.CONTAINER
    return IsolationMode(marker_value.upper())


class VpsInstanceStatus(UpperCaseStrEnum):
    """Status of a VPS instance as reported by the provider API."""

    PENDING = auto()
    ACTIVE = auto()
    HALTED = auto()
    DESTROYING = auto()
    UNKNOWN = auto()
