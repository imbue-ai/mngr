from pathlib import Path
from typing import NoReturn

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts as add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host as create_pyinfra_host
from imbue.mngr.providers.ssh_utils import ensure_per_host_known_hosts_link
from imbue.mngr.providers.ssh_utils import generate_ed25519_host_keypair as generate_ed25519_host_keypair
from imbue.mngr.providers.ssh_utils import generate_ssh_keypair as generate_ssh_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair as load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import (
    load_or_create_per_host_client_keypair as load_or_create_per_host_client_keypair,
)
from imbue.mngr.providers.ssh_utils import load_or_create_per_host_host_keypair as load_or_create_per_host_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair as load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import per_host_key_dir as per_host_key_dir
from imbue.mngr.providers.ssh_utils import resolve_keypair_with_fallback
from imbue.mngr.providers.ssh_utils import save_ssh_keypair as save_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd as wait_for_sshd
from imbue.mngr.providers.ssh_utils import wait_for_sshd_with_retry as wait_for_sshd_with_retry
from imbue.mngr_modal.constants import MODAL_KNOWN_HOSTS_NAME
from imbue.mngr_modal.constants import MODAL_SSH_KEY_NAME
from imbue.mngr_modal.errors import ModalHostKeyMissingError


def resolve_modal_client_keypair(keys_dir: Path, host_id: HostId) -> tuple[Path, str]:
    """Return the client keypair that opens ``host_id``'s sandbox, never creating one.

    The per-host pair minted at create wins. Without one, the instance's
    shared ``keys/modal_ssh_key`` is read (never created): it is the
    operator-installed key that sandboxes created by the TMR CI and mngr-seer
    debug flows authorize through their own ``authorized_keys``. Raises
    ``ModalHostKeyMissingError`` when neither exists: such a sandbox cannot be
    opened from this machine. When the per-host pair wins, its dir gets the
    ``known_hosts`` link key-sibling consumers (the forward SSH tunnel) rely on.
    """
    private_key_path, public_key = resolve_keypair_with_fallback(
        per_host_key_dir(keys_dir, host_id),
        keys_dir,
        MODAL_SSH_KEY_NAME,
        lambda: _raise_modal_client_key_missing(keys_dir, host_id),
    )
    if private_key_path.parent != keys_dir:
        ensure_per_host_known_hosts_link(keys_dir, host_id, MODAL_KNOWN_HOSTS_NAME)
    return private_key_path, public_key


def _raise_modal_client_key_missing(keys_dir: Path, host_id: HostId) -> NoReturn:
    raise ModalHostKeyMissingError(
        f"No client key for sandbox {host_id} under {keys_dir}: neither the per-host key "
        f"({per_host_key_dir(keys_dir, host_id) / MODAL_SSH_KEY_NAME}) nor the shared key "
        f"({keys_dir / MODAL_SSH_KEY_NAME}) exists, so this sandbox cannot be reached from this machine."
    )
