from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair

# SSH key-file names under a provider instance's ``key_dir``. Shared so the
# provider (substrate) and the bare realizer -- which reuses the VPS keys to
# reach the agent on the VM's own port-22 sshd -- refer to the same files.
VPS_SSH_KEY_NAME: Final[str] = "vps_ssh_key"
VPS_HOST_KEY_NAME: Final[str] = "host_key"
VPS_KNOWN_HOSTS_NAME: Final[str] = "vps_known_hosts"

# Subdirectory of a provider instance's ``key_dir`` holding per-host sshd HOST
# keys. Host keys are unique per host -- a host key proves "you reached the host
# you expected", so reusing one across hosts would let a party who holds it
# impersonate any sibling host. (The provider-global *client* keys next to this
# dir are a different matter: they authenticate the operator TO their own hosts,
# so the operator reusing one across their own hosts is not an impersonation
# vector.)
_PER_HOST_KEY_SUBDIR: Final[str] = "host_keys"


def per_host_key_dir(base_key_dir: Path, host_id: HostId) -> Path:
    """Directory holding ``host_id``'s unique sshd host keypair(s)."""
    return base_key_dir / _PER_HOST_KEY_SUBDIR / host_id.get_uuid().hex


def load_or_create_per_host_host_keypair(base_key_dir: Path, host_id: HostId, key_name: str) -> tuple[Path, str]:
    """Load-or-create ``host_id``'s unique sshd host keypair under ``base_key_dir``.

    A fresh host always gets its own keypair, so a host key can never be reused to
    impersonate a different host. Deliberately never falls back to the legacy
    provider-global key -- that fallback lives only in the read-only resume path
    (:func:`read_host_public_key_with_legacy_fallback`) for hosts created before
    per-host keys existed.
    """
    return load_or_create_host_keypair(per_host_key_dir(base_key_dir, host_id), key_name)


def read_host_public_key_with_legacy_fallback(base_key_dir: Path, host_id: HostId, key_name: str) -> str | None:
    """Return ``host_id``'s public host key: per-host if present, else the legacy shared key.

    Read-only (creates nothing). Used by the offline-resume rebind, which must
    reproduce the key the *running* host actually serves: the per-host key for
    hosts created after per-host keys landed, or the provider-global key for older
    hosts that predate them. Returns ``None`` when neither exists.
    """
    per_host_public_key_path = per_host_key_dir(base_key_dir, host_id) / f"{key_name}.pub"
    if per_host_public_key_path.exists():
        return per_host_public_key_path.read_text().strip()
    legacy_public_key_path = base_key_dir / f"{key_name}.pub"
    if legacy_public_key_path.exists():
        return legacy_public_key_path.read_text().strip()
    return None


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
