"""Per-latchkey-directory encryption-key resolution.

Latchkey's credential store is encrypted with a key the operator
historically had to put in ``LATCHKEY_ENCRYPTION_KEY`` (or rely on a
system keychain we don't have on Linux dev boxes). For
minds-managed envs, that's both ergonomically painful (every fresh
shell or env switch needs the right export) and conceptually wrong:
each ``~/.minds-<env>/latchkey/`` has its own credential store, so
each should have its own key.

This module owns the convention: ``<latchkey_directory>/encryption_key``
holds a 32-byte URL-safe base64 key (chmod 0600). It's generated lazily
on first lookup; once written, it's read on every subsequent call.

Operator override: if ``LATCHKEY_ENCRYPTION_KEY`` is set in
``os.environ`` at lookup time, it wins over the per-env file. That
preserves the existing single-key-across-everything workflow for
operators who want it, while making "just run ``minds run``" work
out of the box for everyone else.
"""

import os
import secrets
import stat
from pathlib import Path
from typing import Final

from pydantic import SecretStr

# Operator-supplied global key. Reading it at module level (vs at lookup
# time) would freeze it on import; we read at lookup time so a freshly
# exported value takes effect for the next ``minds run``.
LATCHKEY_ENCRYPTION_KEY_ENV_VAR: Final[str] = "LATCHKEY_ENCRYPTION_KEY"

# Filename under ``latchkey_directory`` holding the per-env key. Plain
# text; restricted to owner-read/write via chmod 0600 at write time.
ENCRYPTION_KEY_FILENAME: Final[str] = "encryption_key"

# Number of random bytes -> URL-safe base64 length is ~43 chars. Matches
# the entropy of the ``openssl rand -base64 32`` snippet upstream
# latchkey suggests when its own keychain probe fails.
_ENCRYPTION_KEY_BYTES: Final[int] = 32

# Group/other access bits we refuse to read the key file with. Any
# bit set in this mask means the file is readable, writable, or
# executable by something other than the owner -- a secret of this
# sensitivity must never be that exposed.
_FORBIDDEN_PERMISSION_BITS: Final[int] = stat.S_IRWXG | stat.S_IRWXO


class LatchkeyEncryptionKeyPermissionError(PermissionError):
    """Raised when ``<latchkey_directory>/encryption_key`` is readable or writable by non-owner.

    A standalone subclass of :class:`PermissionError` (not :class:`LatchkeyError`)
    so this module stays import-free of the rest of the package and avoids
    a circular import with ``core.py``. Callers that need a
    :class:`LatchkeyError`-shaped error (e.g. for ``click``'s exception
    translator) should catch this and re-raise.
    """


def encryption_key_path(latchkey_directory: Path) -> Path:
    return latchkey_directory / ENCRYPTION_KEY_FILENAME


def load_or_create_encryption_key(latchkey_directory: Path) -> SecretStr:
    """Return the encryption key for ``latchkey_directory``, creating it on first call.

    Precedence:

    1. ``LATCHKEY_ENCRYPTION_KEY`` in :data:`os.environ` (operator
       global override). Returned verbatim; the per-env key file is
       not consulted or created.
    2. ``<latchkey_directory>/encryption_key`` if it exists. The
       file's permission bits are validated -- any group or other
       access bit set raises :class:`LatchkeyEncryptionKeyPermissionError`
       so an operator who relaxed the mode after a clipboard-paste or
       a stray ``chmod -R`` finds out loudly instead of silently
       leaking the key to other local users.
    3. Generate a fresh URL-safe base64 32-byte key, write to
       ``<latchkey_directory>/encryption_key`` with mode 0600, and
       return.

    Idempotent: re-calls return the same key as long as the file is
    intact. Cross-process safe: the ``open(..., "x")`` exclusive create
    avoids two simultaneous minds runs racing to mint different keys
    (loser falls through to the read path).

    Raises:
        LatchkeyEncryptionKeyPermissionError: when an existing key file
            has group or other access bits set. Does not fire when the
            operator override is in effect (the file is not consulted)
            or when this call freshly minted the file (we wrote it 0600
            ourselves).
    """
    operator_override = os.environ.get(LATCHKEY_ENCRYPTION_KEY_ENV_VAR)
    if operator_override:
        return SecretStr(operator_override)

    key_path = encryption_key_path(latchkey_directory)
    if key_path.is_file():
        _validate_key_file_permissions(key_path)
        return SecretStr(key_path.read_text(encoding="utf-8").strip())

    latchkey_directory.mkdir(parents=True, exist_ok=True)
    fresh_key = secrets.token_urlsafe(_ENCRYPTION_KEY_BYTES)
    # Race-safe write: ``x`` mode raises FileExistsError if another
    # process beat us to it -- in which case we fall through and read
    # the loser-of-the-race version (whose permissions we then validate
    # for the same reason we'd validate any other pre-existing file).
    try:
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError:
        _validate_key_file_permissions(key_path)
        return SecretStr(key_path.read_text(encoding="utf-8").strip())
    try:
        os.write(fd, fresh_key.encode("utf-8"))
    finally:
        os.close(fd)
    return SecretStr(fresh_key)


def _validate_key_file_permissions(key_path: Path) -> None:
    """Refuse to read ``key_path`` if any group/other access bit is set.

    Owner-only (0600, 0400, 0700, ...) is accepted; anything that
    grants group or other any access raises. The check is on the live
    on-disk mode each call, so a key minted 0600 that gets later
    relaxed to 0644 is rejected on the next load -- the operator must
    re-chmod it back before minds can use it.
    """
    file_mode = key_path.stat().st_mode & 0o777
    forbidden = file_mode & _FORBIDDEN_PERMISSION_BITS
    if forbidden:
        raise LatchkeyEncryptionKeyPermissionError(
            f"Latchkey encryption-key file {key_path} has unsafe permissions "
            f"(mode {file_mode:o}, must be owner-only). Run "
            f"'chmod 600 {key_path}' to restrict it, then retry."
        )
