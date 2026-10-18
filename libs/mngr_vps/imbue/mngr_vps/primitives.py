from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.providers.host_access_manifest import HostAccessManifest
from imbue.mngr.providers.host_access_manifest import PerHostDirScheme
from imbue.mngr.providers.host_access_manifest import keypair_file_names

# The per-host key helpers moved to mngr core (shared with docker/modal/lima);
# re-exported here so vps callers keep their historical import path.
from imbue.mngr.providers.ssh_utils import load_or_create_per_host_host_keypair as load_or_create_per_host_host_keypair
from imbue.mngr.providers.ssh_utils import per_host_key_dir as per_host_key_dir
from imbue.mngr.providers.ssh_utils import (
    read_host_public_key_with_legacy_fallback as read_host_public_key_with_legacy_fallback,
)

# SSH key-file names under a provider instance's ``key_dir``. ``vps_ssh_key``
# is the VM-root client key, minted per host under ``host_keys/<hex>/`` at
# create; the bare realizer's agent endpoint is that same key. The known_hosts
# file stays provider-wide (its pin store attributes pins per host).
VPS_SSH_KEY_NAME: Final[str] = "vps_ssh_key"
VPS_HOST_KEY_NAME: Final[str] = "host_key"
VPS_KNOWN_HOSTS_NAME: Final[str] = "vps_known_hosts"
# The container placement's client/host keys and its known_hosts, under the same key dir.
CONTAINER_SSH_KEY_NAME: Final[str] = "container_ssh_key"
CONTAINER_HOST_KEY_NAME: Final[str] = "container_host_key"
CONTAINER_KNOWN_HOSTS_NAME: Final[str] = "container_known_hosts"
VPS_KEY_DIR_NAME: Final[str] = "keys"

# The per-host layout contract (README, "SSH key material"), published for
# consumers that carry a host's access material to another machine. The
# VPS-root key is the agent key: under bare placement it opens the agent
# endpoint, and it is the one key every host has minted per host. The per-host
# known_hosts link names the container placement's store (the default); bare
# placement links the VPS store itself (``BareRealizer._agent_ssh_keypair``).
VPS_HOST_ACCESS_MANIFEST: Final[HostAccessManifest] = HostAccessManifest(
    key_dir_name=VPS_KEY_DIR_NAME,
    per_host_dir_scheme=PerHostDirScheme.HOST_KEYS_UUID_HEX,
    file_names=keypair_file_names(
        VPS_SSH_KEY_NAME, CONTAINER_SSH_KEY_NAME, VPS_HOST_KEY_NAME, CONTAINER_HOST_KEY_NAME
    ),
    agent_key_file_name=VPS_SSH_KEY_NAME,
    pin_store_names=(VPS_KNOWN_HOSTS_NAME, CONTAINER_KNOWN_HOSTS_NAME),
    is_pin_store_per_host=False,
    known_hosts_link_store_name=CONTAINER_KNOWN_HOSTS_NAME,
)


class VpsInstanceId(NonEmptyStr):
    """Unique identifier for a VPS instance as assigned by the provider."""


class VpsVcpuCount(PositiveInt):
    """The number of vCPUs a cloud instance shape has. Must be > 0."""


class VpsMemoryMib(PositiveInt):
    """The RAM of a cloud instance shape, in MiB. Must be > 0."""


class VpsDiskGb(PositiveInt):
    """The size of a cloud instance's root disk, in GB as the cloud reports it. Must be > 0."""


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

# Instance-level identity marker carrying the mngr host id, stamped at create
# and read back by discovery (again without SSH) to pick the per-host client key
# that opens the VPS. Providers whose tag charset cannot hold it (GCE labels)
# store the same key/value in instance metadata instead.
HOST_ID_TAG_KEY: Final[str] = "mngr-host-id"


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
