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
        machine_gateway_password      the machine's own password     (owned)
        config.json                   the desktop's settings, with the
                                      machine's own custom services (owned)
        permissions.json           -> latchkey_permissions.json
        browser_state.json.enc     -> the desktop's browser state
        encryption_key             -> the desktop's encryption key
        last-daily-count           -> the desktop's usage-ping stamp

What is *shared* with the desktop is shared by symlink, so there is one copy on
disk and no reconciliation. The links are relative, so the tree survives being
moved or copied.

The config is the one file that is neither owned outright nor shared. The
upstream CLI reads it for the browser this computer signs in with and for the
services it can name, and the custom services a machine can name are its own:
registered on it one at a time, by any of the user's computers, and read back
from it (see :func:`~imbue.mngr_latchkey.remote._transfer.adopt_machine_custom_services`).
So the store's copy is rebuilt from the desktop's file with the machine's
custom services in place of this computer's own
(:func:`~imbue.mngr_latchkey.core.build_config_with_custom_services`), whenever
either side changes.

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
  must never be handed to :meth:`Latchkey.initialize` (which would merge this
  package's half of the config into the store's copy as though it were the
  desktop's). Only the credential/service-introspection subset of
  :class:`Latchkey` is meaningful against one.

* **Nothing may replace a shared link with a regular file.** Writes must go
  *through* the symlinks (``atomic_write_bytes`` resolves them before
  renaming); a plain write-and-rename would silently fork the shared state.
  The one link deliberately replaced is the config's, by
  :func:`_materialize_config`, because the config is no longer shared: a store
  from a build that linked it to the desktop's file is migrated to a file of
  its own holding what the link resolved to.
"""

import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import JsonValue
from pydantic import SecretStr

from imbue.mngr.primitives import HostId
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import atomic_write_bytes
from imbue.mngr_latchkey.core import BROWSER_STATE_FILENAME
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import DAILY_COUNT_STAMP_FILENAME
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.core import build_config_with_custom_services
from imbue.mngr_latchkey.core import custom_service_registration_entries
from imbue.mngr_latchkey.encryption_key import ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host

# Files a machine store shares with the desktop's own latchkey directory, by
# relative symlink. See the module docstring: the encryption key is what makes
# the browser state shareable. The daily-count stamp is shared because it
# rate-limits a ping that is about the *user*: left per-directory, an ordinary
# offline read against each machine store would ping once a day per remote
# host. Its link may dangle until the first ping -- upstream writes the stamp
# through it, creating the desktop's copy -- so unlike the encryption key it
# needs nothing created up front.
SHARED_WITH_DESKTOP_FILENAMES: Final[tuple[str, ...]] = (
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

# Holds the listen password the *remote machine's* gateway holds its callers to,
# durable for the same reason as the key above: the machine keeps its own copy
# only in RAM. It cannot be re-derived here, because it is not this computer's
# to decide -- the workspaces on that machine present it from a host env file
# written once at ``mngr create``, so whichever of the user's computers created
# them fixed the value for the machine's whole life.
_MACHINE_GATEWAY_PASSWORD_FILENAME: Final[str] = "machine_gateway_password"

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


def machine_gateway_password_path(data_dir: Path, host_id: HostId) -> Path:
    """Return the path to the desktop's copy of ``host_id``'s own gateway listen password."""
    return machine_store_dir(data_dir, host_id) / _MACHINE_GATEWAY_PASSWORD_FILENAME


def stored_machine_encryption_key(data_dir: Path, host_id: HostId) -> SecretStr | None:
    """Return the key ``host_id``'s machine keeps its own store under, or ``None`` if unknown.

    ``None`` means no provisioning pass from this desktop has reached the
    machine yet (the pass that mints a fresh machine's key, and adopts the key
    of one provisioned from another of the user's computers), so nothing can be
    encrypted *for* it yet -- not that it has no key.

    Raises:
        LatchkeyStoreError: when the key file exists but cannot be read.
    """
    key = _read_machine_secret(machine_key_path(data_dir, host_id), "machine encryption key")
    return SecretStr(key) if key is not None else None


def stored_machine_gateway_password(data_dir: Path, host_id: HostId) -> str | None:
    """Return the listen password ``host_id``'s machine holds its callers to, or ``None`` if unknown.

    ``None`` means no provisioning pass from this desktop has ever seen the
    machine's password -- not that it has none.

    Raises:
        LatchkeyStoreError: when the password file exists but cannot be read.
    """
    return _read_machine_secret(machine_gateway_password_path(data_dir, host_id), "machine gateway listen password")


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
    _write_machine_secret(machine_key_path(data_dir, host_id), key.get_secret_value(), "machine encryption key")


def store_machine_gateway_password(data_dir: Path, host_id: HostId, password: str) -> None:
    """Record the listen password ``host_id``'s machine holds its callers to.

    Durable for the same reason as the key, and follows the machine the same
    way: it is what a rebooted machine (whose RAM copy is gone) is handed back,
    so that the workspaces on it keep authenticating with the password they were
    created with even when the computer that created them is not the one
    re-provisioning.

    Raises:
        LatchkeyStoreError: when the password cannot be written.
    """
    _write_machine_secret(
        machine_gateway_password_path(data_dir, host_id), password, "machine gateway listen password"
    )


def _read_machine_secret(path: Path, description: str) -> str | None:
    """Return the stripped content of one recorded machine secret, or ``None`` when it is not recorded.

    An empty file reads as not recorded: a truncated record is no more usable
    than an absent one, and answering with an empty secret would hand it to a
    gateway (or encrypt a transfer under it) as though it were real.
    """
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to read the {description} at {path}: {e}") from e


def _write_machine_secret(path: Path, value: str, description: str) -> None:
    """Record one machine secret, owner-readable only."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, value)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to store the {description} at {path}: {e}") from e


def materialize_machine_store(latchkey_directory: Path, data_dir: Path, host_id: HostId) -> Path:
    """Create (or repair) ``host_id``'s machine store and return its directory.

    Idempotent: an already-materialized store is left as it is, a link that
    points somewhere else is re-pointed, and the store's config is rebuilt
    around the custom services it names (see :func:`_materialize_config`).
    Materializing does *not* create the credential store -- a machine store
    with no credentials yet is the ordinary state of a machine nobody has
    connected anything to -- but it does give the directory a format stamp, so
    which format a store here would be read as is settled before there is one.

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
    _materialize_config(latchkey_directory, store_dir)
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
    credentials, and reading the custom services registered on it. Never hand
    the result to :meth:`Latchkey.initialize` or to anything that reads
    :attr:`Latchkey.plugin_data_dir` -- under a machine store that resolves to
    a nested ``mngr_latchkey/`` of its own. Pass the desktop's
    :class:`Latchkey` for those.

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


def write_machine_store_config(
    latchkey_directory: Path, store_dir: Path, custom_service_entries: Mapping[str, JsonValue]
) -> bool:
    """Make the machine store's ``config.json`` the desktop's settings plus ``custom_service_entries``.

    What a registration, a deregistration, and an adopt of what the machine
    holds all come down to (see :func:`~imbue.mngr_latchkey.core.build_config_with_custom_services`).
    Written only when it differs, so an unchanged set costs nothing; returns
    whether it was written, which is what tells a caller the catalog the
    desktop gateway validates against has to be refreshed.

    Raises:
        LatchkeyStoreError: when the desktop's config is not one the store's
            can be built from, or the store's cannot be written.
    """
    desktop_config_path = latchkey_directory / CONFIG_FILENAME
    config_path = store_dir / CONFIG_FILENAME
    try:
        desktop_config = desktop_config_path.read_text(encoding="utf-8") if desktop_config_path.is_file() else None
        content = build_config_with_custom_services(desktop_config, custom_service_entries)
        if config_path.is_file() and config_path.read_text(encoding="utf-8") == content:
            return False
        atomic_write(config_path, content)
    except (OSError, LatchkeyError) as e:
        raise LatchkeyStoreError(
            f"Failed to write the latchkey config of the machine store at {store_dir}: {e}"
        ) from e
    return True


def _materialize_config(latchkey_directory: Path, store_dir: Path) -> None:
    """Give the machine store a ``config.json`` of its own, kept current with the desktop's settings.

    Rebuilt on every materialization around the custom services the store
    already names, so a browser or keyring setting that changed in the
    desktop's file reaches the store, and a service registered on the machine
    is kept. A store that has none yet starts with none: what the machine
    holds is adopted on the next read of it.

    CLEANUP: drop the symlink branch once no machine store from a build that
    shared the desktop's config by link remains. Such a link resolves to the
    desktop's file, so the store starts with the custom services this computer
    registered -- which the machine was handed as a snapshot by every connect
    those builds made, so it is what the machine holds too.

    Raises:
        LatchkeyStoreError: when the config cannot be built or written.
    """
    config_path = store_dir / CONFIG_FILENAME
    entries = custom_service_registration_entries(store_dir) if config_path.is_file() else {}
    if config_path.is_symlink():
        try:
            config_path.unlink()
        except OSError as e:
            raise LatchkeyStoreError(f"Failed to replace the shared config link at {config_path}: {e}") from e
    write_machine_store_config(latchkey_directory, store_dir, entries)


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
