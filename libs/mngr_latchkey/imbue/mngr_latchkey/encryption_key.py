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


def encryption_key_path(latchkey_directory: Path) -> Path:
    return latchkey_directory / ENCRYPTION_KEY_FILENAME


def load_or_create_encryption_key(latchkey_directory: Path) -> SecretStr:
    """Return the encryption key for ``latchkey_directory``, creating it on first call.

    Precedence:

    1. ``LATCHKEY_ENCRYPTION_KEY`` in :data:`os.environ` (operator
       global override). Returned verbatim; the per-env key file is
       not consulted or created.
    2. ``<latchkey_directory>/encryption_key`` if it exists. Read +
       returned. File permissions are *not* validated -- if the
       operator chmod'd it back to 0644 we still trust them.
    3. Generate a fresh URL-safe base64 32-byte key, write to
       ``<latchkey_directory>/encryption_key`` with mode 0600, and
       return.

    Idempotent: re-calls return the same key as long as the file is
    intact. Cross-process safe: the ``open(..., "x")`` exclusive create
    avoids two simultaneous minds runs racing to mint different keys
    (loser falls through to the read path).
    """
    operator_override = os.environ.get(LATCHKEY_ENCRYPTION_KEY_ENV_VAR)
    if operator_override:
        return SecretStr(operator_override)

    key_path = encryption_key_path(latchkey_directory)
    if key_path.is_file():
        return SecretStr(key_path.read_text(encoding="utf-8").strip())

    latchkey_directory.mkdir(parents=True, exist_ok=True)
    fresh_key = secrets.token_urlsafe(_ENCRYPTION_KEY_BYTES)
    # Race-safe write: ``x`` mode raises FileExistsError if another
    # process beat us to it -- in which case we fall through and read
    # the loser-of-the-race version.
    try:
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError:
        return SecretStr(key_path.read_text(encoding="utf-8").strip())
    try:
        os.write(fd, fresh_key.encode("utf-8"))
    finally:
        os.close(fd)
    return SecretStr(fresh_key)
