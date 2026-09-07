"""Per-machine latchkey stores: one ``LATCHKEY_DIRECTORY`` per remote host.

A remote host's credentials belong to *its own machine* (the VPS), not to the
user's computer: only the machine that holds a refresh token may rotate it, and
a machine that can rotate its own tokens keeps working while the user's computer
is offline. The VPS's ``~/.latchkey`` is therefore the source of truth for that
host, and the desktop keeps a local **machine store** -- a mirror that is also a
usable ``LATCHKEY_DIRECTORY``, so every read a caller needs (``services info
--offline``, ``auth list --offline``) is answered from disk instead of over the
network.

The machine store is the per-host directory this plugin already owns::

    <latchkey_directory>/mngr_latchkey/hosts/<host_id>/
        credentials.json.enc          mirror of the machine's store  (owned)
        data-format-version           the mirror's own format stamp  (owned)
        latchkey_permissions.json     canonical per-host policy      (owned)
        machine_encryption_key        the machine's own key          (owned)
        permissions.json           -> latchkey_permissions.json
        config.json                -> the desktop's config.json
        browser_state.json.enc     -> the desktop's browser state
        encryption_key             -> the desktop's encryption key
        last-daily-count           -> the desktop's usage-ping stamp

What is *shared* with the desktop is shared by symlink, so there is one copy on
disk and no reconciliation. The links are relative, so the tree survives being
moved or copied.

Three things are worth knowing:

* **A mirror is held under the desktop's key, not the mirrored machine's.**
  Upstream encrypts the browser state with the same per-directory key as the
  credential store, so a machine store can only reuse the desktop's browser
  session -- which is what keeps signing a second machine in to a service down
  to a consent click rather than a full re-login -- while it also uses the
  desktop's key. Each machine keeps its own key for its own store; the key
  changes at the transfer boundary instead, by re-encrypting on the way in and
  on the way out.

* **A machine store is not a plugin root.** ``Latchkey.plugin_data_dir`` would
  resolve to a *nested* ``mngr_latchkey/`` underneath it, so a machine store
  must never be handed to :meth:`Latchkey.initialize` (which also rewrites
  ``config.json`` -- here a symlink into the desktop's). Only the
  credential/service-introspection subset of :class:`Latchkey` is meaningful
  against one.

* **Nothing may replace a link with a regular file.** Writes must go *through*
  the symlinks (``atomic_write_bytes`` resolves them before renaming); a plain
  write-and-rename would silently fork the shared state.
"""

import os
import secrets
import stat
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from imbue.mngr.primitives import HostId
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import atomic_write_bytes
from imbue.mngr_latchkey.core import BROWSER_STATE_FILENAME
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import DAILY_COUNT_STAMP_FILENAME
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.encryption_key import ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host

# Files a machine store shares with the desktop's own latchkey directory, by
# relative symlink. See the module docstring: the encryption key is what makes
# the browser state shareable, and the config carries the hidden built-in
# services and the custom-service registrations every gateway needs. The
# daily-count stamp is shared because it rate-limits a ping that is about the
# *user*: left per-directory, an ordinary offline read against each machine
# store would ping once a day per remote host. Its link may dangle until the
# first ping -- upstream writes the stamp through it, creating the desktop's
# copy -- so unlike the encryption key it needs nothing created up front.
SHARED_WITH_DESKTOP_FILENAMES: Final[tuple[str, ...]] = (
    CONFIG_FILENAME,
    BROWSER_STATE_FILENAME,
    ENCRYPTION_KEY_FILENAME,
    DAILY_COUNT_STAMP_FILENAME,
)

# Owner-only, like every other directory holding credential material.
_MACHINE_STORE_DIR_MODE: Final[int] = stat.S_IRWXU

# Holds the encryption key the *remote machine* keeps its own store under -- not
# the key this directory's own mirror uses, which is the desktop's (see the
# module docstring). The machine's copy lives only in its RAM-backed secrets
# directory, so this file is the durable one: without it, a rebooted machine's
# store could never be read again. It sits inside the machine store so that
# dropping the store drops the key with it, which is exactly right -- one is
# worthless without the other.
_MACHINE_KEY_FILENAME: Final[str] = "machine_encryption_key"

# Same 32 random bytes, URL-safe base64, that ``load_or_create_encryption_key``
# mints for the desktop.
_MACHINE_KEY_BYTES: Final[int] = 32


def machine_store_dir(data_dir: Path, host_id: HostId) -> Path:
    """Return the machine store directory for ``host_id``.

    ``data_dir`` is the plugin data dir (see
    :func:`~imbue.mngr_latchkey.store.plugin_data_dir`). The machine store is
    the same per-host directory the canonical permissions file lives in, so the
    two can never drift apart.
    """
    return permissions_path_for_host(data_dir, host_id).parent


def machine_credentials_path(data_dir: Path, host_id: HostId) -> Path:
    """Return the path to the mirrored credential store for ``host_id``."""
    return machine_store_dir(data_dir, host_id) / CREDENTIALS_STORE_FILENAME


def machine_key_path(data_dir: Path, host_id: HostId) -> Path:
    """Return the path to the desktop's copy of ``host_id``'s own encryption key."""
    return machine_store_dir(data_dir, host_id) / _MACHINE_KEY_FILENAME


def stored_machine_encryption_key(data_dir: Path, host_id: HostId) -> SecretStr | None:
    """Return the key ``host_id``'s machine keeps its own store under, or ``None`` if unknown.

    ``None`` means no provisioning pass from this desktop has reached the
    machine yet (the pass that mints a fresh machine's key, and adopts the key
    of one provisioned from another of the user's computers), so nothing can be
    encrypted *for* it yet -- not that it has no key.

    Raises:
        LatchkeyStoreError: when the key file exists but cannot be read.
    """
    key_path = machine_key_path(data_dir, host_id)
    if not key_path.is_file():
        return None
    try:
        return SecretStr(key_path.read_text(encoding="utf-8").strip())
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to read the machine encryption key at {key_path}: {e}") from e


def generate_machine_encryption_key() -> SecretStr:
    """Mint a fresh key for a machine's own credential store."""
    return SecretStr(secrets.token_urlsafe(_MACHINE_KEY_BYTES))


def store_machine_encryption_key(data_dir: Path, host_id: HostId, key: SecretStr) -> None:
    """Record the key ``host_id``'s machine keeps its own store under.

    A durable mirror of the machine's own copy, which lives in RAM and does not
    survive a reboot: this record is what lets a rebooted machine be handed
    back the key its store is already encrypted with. It follows the machine,
    not the other way around -- provisioning rewrites it whenever the machine
    turns out to be running under a key another of the user's computers gave it.

    Raises:
        LatchkeyStoreError: when the key cannot be written.
    """
    key_path = machine_key_path(data_dir, host_id)
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(key_path, key.get_secret_value())
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to store the machine encryption key at {key_path}: {e}") from e


def materialize_machine_store(latchkey_directory: Path, data_dir: Path, host_id: HostId) -> Path:
    """Create (or repair) ``host_id``'s machine store and return its directory.

    Idempotent: an already-materialized store is left as it is, and a link that
    points somewhere else is re-pointed. Materializing does *not* create the
    credential store -- a machine store with no credentials yet is the ordinary
    state of a machine nobody has connected anything to -- but it does give the
    directory a format stamp, so which format a store here would be read as is
    settled before there is one.

    The desktop's encryption key is created if it does not exist yet, so the
    link never dangles: a dangling key link would make the upstream CLI mint a
    *second* key inside the machine store and quietly encrypt its credentials
    with a key nothing else uses.

    Raises:
        LatchkeyStoreError: when a shared name is occupied by a regular file
            (which would mean an independent copy of state that is supposed to
            be shared, so it is reported rather than deleted), or when the
            directory or a link cannot be created.
    """
    load_or_create_encryption_key(latchkey_directory)
    store_dir = machine_store_dir(data_dir, host_id)
    try:
        store_dir.mkdir(parents=True, exist_ok=True)
        store_dir.chmod(_MACHINE_STORE_DIR_MODE)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to create the machine store at {store_dir}: {e}") from e

    for filename in SHARED_WITH_DESKTOP_FILENAMES:
        _link(store_dir / filename, latchkey_directory / filename)
    _seed_data_format_stamp(latchkey_directory, store_dir)
    # The upstream CLI reads a directory's policy from ``permissions.json``,
    # while this plugin's canonical per-host file keeps its own name; linking
    # them means a bare ``latchkey`` run against a machine store enforces
    # exactly what that host's agents are held to.
    _link(store_dir / PERMISSIONS_CONFIG_FILENAME, permissions_path_for_host(data_dir, host_id))
    return store_dir


def latchkey_for_machine(latchkey: Latchkey, data_dir: Path, host_id: HostId) -> Latchkey:
    """Return a :class:`Latchkey` bound to ``host_id``'s machine store.

    The narrow use of a machine store: reading and writing *that machine's*
    credentials. Never hand the result to :meth:`Latchkey.initialize` or to
    anything that reads :attr:`Latchkey.plugin_data_dir` -- under a machine store
    that resolves to a nested ``mngr_latchkey/`` of its own, and initialization
    would rewrite the ``config.json`` this directory shares with the desktop.
    Pass the desktop's :class:`Latchkey` for those.

    Raises:
        LatchkeyStoreError: when the machine store cannot be materialized.
    """
    store_dir = materialize_machine_store(latchkey.latchkey_directory, data_dir, host_id)
    return Latchkey(latchkey_directory=store_dir, latchkey_binary=latchkey.latchkey_binary)


def write_machine_credentials(store_dir: Path, content: bytes, data_format_version: str) -> None:
    """Replace the machine store's credential mirror and its format stamp.

    The stamp is written first, for the same reason the VPS sync writes it
    first: a stamp older than the store it describes makes the upstream CLI
    "migrate" an already-current store and corrupt it, while the reverse
    ordering only leaves the mirror unreadable until the next write.

    Raises:
        LatchkeyStoreError: when either write fails.
    """
    try:
        atomic_write(store_dir / UPSTREAM_DATA_FORMAT_VERSION_FILENAME, data_format_version)
        atomic_write_bytes(store_dir / CREDENTIALS_STORE_FILENAME, content)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to write the credential mirror in {store_dir}: {e}") from e


def clear_machine_credentials(store_dir: Path) -> None:
    """Remove the machine store's credential mirror, leaving the store itself.

    Used when the machine holds nothing at all, so that "no credentials" is
    represented the same way locally as it is on the machine itself, rather than
    by a mirror nobody updated.

    Raises:
        LatchkeyStoreError: when the mirror exists but cannot be removed.
    """
    try:
        (store_dir / CREDENTIALS_STORE_FILENAME).unlink(missing_ok=True)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to clear the credential mirror in {store_dir}: {e}") from e


def _seed_data_format_stamp(latchkey_directory: Path, store_dir: Path) -> None:
    """Give a stampless machine store the desktop's format stamp.

    The stamp records which format the store in this directory is in, so it
    belongs to the store and travels with it (a transfer writes both). A
    directory that has never held one starts from the format the desktop CLI
    writes, which is what produces anything that lands here.

    Raises:
        LatchkeyStoreError: when the stamp cannot be copied.
    """
    stamp_path = store_dir / UPSTREAM_DATA_FORMAT_VERSION_FILENAME
    desktop_stamp_path = latchkey_directory / UPSTREAM_DATA_FORMAT_VERSION_FILENAME
    if stamp_path.exists() or not desktop_stamp_path.is_file():
        return
    try:
        atomic_write(stamp_path, desktop_stamp_path.read_text())
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to seed the data-format stamp in {store_dir}: {e}") from e


def _link(link_path: Path, target_path: Path) -> None:
    """Point ``link_path`` at ``target_path`` with a relative symlink, idempotently."""
    relative_target = os.path.relpath(target_path, link_path.parent)
    is_existing_link = link_path.is_symlink()
    if not is_existing_link and link_path.exists():
        raise LatchkeyStoreError(
            f"Refusing to share {link_path} with {target_path}: a regular file is already there. "
            "It holds state that is meant to be shared with the desktop's latchkey directory; "
            "move it aside to adopt the shared copy."
        )
    if is_existing_link:
        if os.readlink(link_path) == relative_target:
            return
        # Pointed somewhere else: re-point it rather than leave a machine store
        # reading state that is not the desktop's.
        link_path.unlink()
    try:
        link_path.symlink_to(relative_target)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to link {link_path} to {target_path}: {e}") from e
