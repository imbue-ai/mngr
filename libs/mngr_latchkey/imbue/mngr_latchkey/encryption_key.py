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
import tempfile
from contextlib import suppress
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


def inject_encryption_key_into_env(env: dict[str, str], encryption_key: SecretStr | None) -> None:
    """Set ``LATCHKEY_ENCRYPTION_KEY`` in ``env`` from the per-directory key.

    An operator-set value already present in ``env`` (typically inherited
    from ``os.environ``) always wins; the per-directory key only fills the
    var in when the operator has not set one globally. Passing this var on
    every latchkey spawn keeps Latchkey's startup key-resolution from ever
    falling through to the system keychain (which on macOS pops a keychain
    access dialog).
    """
    if encryption_key is None:
        return
    if env.get(LATCHKEY_ENCRYPTION_KEY_ENV_VAR):
        return
    env[LATCHKEY_ENCRYPTION_KEY_ENV_VAR] = encryption_key.get_secret_value()


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
    intact. Cross-process safe via an atomic ``os.link``-based publish:
    the key is fully written and ``fsync``ed into a temp file first,
    then linked into the final path. The final path therefore only
    ever exists with complete contents, so a concurrent reader that
    sees it can never observe an empty or partially-written key. The
    link itself fails with ``FileExistsError`` if another process
    already published, in which case we read that version.

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
        # Safe to read: the atomic-link publish below means ``key_path``
        # only appears once its contents are complete.
        _validate_key_file_permissions(key_path)
        return SecretStr(key_path.read_text(encoding="utf-8").strip())

    latchkey_directory.mkdir(parents=True, exist_ok=True)
    fresh_key = secrets.token_urlsafe(_ENCRYPTION_KEY_BYTES)
    return _atomically_publish_key(key_path, fresh_key)


def _atomically_publish_key(key_path: Path, fresh_key: str) -> SecretStr:
    """Write ``fresh_key`` into a sibling temp file and ``os.link`` it to ``key_path``.

    The temp file is created with mode 0600 (via :func:`tempfile.mkstemp`),
    fully written, ``fsync``ed, and closed *before* the link. ``os.link``
    is atomic and fails with :class:`FileExistsError` if ``key_path``
    already exists -- so:

    * On link success, no reader could have observed ``key_path`` in an
      intermediate state: it sprang into existence already complete.
    * On link failure, the loser of the race unlinks its temp file and
      reads the winner's (also complete) key. The winner's file is
      guaranteed complete by the same argument applied to *their* link
      call.

    This replaces an earlier ``O_EXCL`` open + write-to-final-path
    scheme that left a window during which the final path existed but
    was empty, allowing a concurrent caller to read ``""`` as the key.
    """
    # ``mkstemp`` creates the file with mode 0600 and returns an open
    # fd. Keeping the temp file in the same directory as ``key_path``
    # is required for ``os.link`` (same filesystem) and is also what
    # makes the link atomic.
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{ENCRYPTION_KEY_FILENAME}.", suffix=".tmp", dir=str(key_path.parent)
    )
    tmp_path = Path(tmp_path_str)
    try:
        try:
            os.write(tmp_fd, fresh_key.encode("utf-8"))
            # fsync before the link so a crash between link and the
            # data hitting disk can't leave a zero-length final file.
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        try:
            os.link(str(tmp_path), str(key_path))
        except FileExistsError:
            # Another process published first. Their file is complete
            # (same argument, applied to their link call), so read it.
            _validate_key_file_permissions(key_path)
            return SecretStr(key_path.read_text(encoding="utf-8").strip())
        return SecretStr(fresh_key)
    finally:
        # Always remove the temp file: on success it's a redundant
        # second link to the same inode; on failure it's debris.
        with suppress(FileNotFoundError):
            tmp_path.unlink()


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
