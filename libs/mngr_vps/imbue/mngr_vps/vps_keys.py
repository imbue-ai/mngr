from pathlib import Path
from typing import NoReturn

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import per_host_key_dir
from imbue.mngr.providers.ssh_utils import resolve_keypair_with_fallback
from imbue.mngr_vps.data_types import VpsRootKeypair
from imbue.mngr_vps.errors import VpsHostKeyMissingError
from imbue.mngr_vps.primitives import VPS_SSH_KEY_NAME


def mint_vps_root_keypair(key_dir: Path, host_id: HostId) -> tuple[Path, str]:
    """Create (or load) ``host_id``'s own VPS-root client keypair; the create path only.

    Written straight into the per-host key dir with no ``known_hosts`` sibling
    link: that link is owned by the agent endpoint's key (the container key, or
    this same key under bare placement, where the bare realizer creates it), so
    the sibling convention always points at the store for the endpoint the key
    actually opens.
    """
    return load_or_create_ssh_keypair(per_host_key_dir(key_dir, host_id), VPS_SSH_KEY_NAME)


def resolve_vps_root_keypair(key_dir: Path, host_id: HostId) -> VpsRootKeypair:
    """Return the client keypair that opens ``host_id``'s VPS root, never creating one.

    The per-host pair minted at create wins. Hosts created before per-host
    VPS-root keys existed only authorize the provider-wide ``vps_ssh_key``,
    which is read (never created) as a fallback so they stay reachable from the
    machine that created them; the result says when that fallback was taken.
    Raises ``VpsHostKeyMissingError`` when neither exists: such a host cannot
    be opened from this machine.
    """
    # CLEANUP: pass ``key_dir`` itself as the fallback dir (i.e. drop the legacy
    # read fallback) once hosts created before per-host VPS-root keys
    # (mngr_vps 0.5.x) are no longer expected to exist.
    private_key_path, public_key = resolve_keypair_with_fallback(
        per_host_key_dir(key_dir, host_id),
        key_dir,
        VPS_SSH_KEY_NAME,
        lambda: _raise_vps_root_key_missing(key_dir, host_id),
    )
    return VpsRootKeypair(
        private_key_path=private_key_path,
        public_key=public_key,
        is_legacy_provider_key=private_key_path.parent == key_dir,
    )


def _raise_vps_root_key_missing(key_dir: Path, host_id: HostId) -> NoReturn:
    raise VpsHostKeyMissingError(
        f"No VPS-root client key for host {host_id} under {key_dir}: neither the per-host key "
        f"({per_host_key_dir(key_dir, host_id) / VPS_SSH_KEY_NAME}) nor a legacy provider-wide key exists, "
        "so this host cannot be reached from this machine (it was created elsewhere, or its key material was removed)."
    )
