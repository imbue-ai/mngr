"""Storage helper for the single central ``MINDS_API_KEY``.

There is one ``MINDS_API_KEY`` per minds installation, persisted in
``<data_dir>/minds_api_key``. It is generated lazily on first use,
re-used across desktop-client restarts, and handed to:

* the latchkey gateway's bundled ``minds-api-proxy`` extension (via the
  ``LATCHKEY_EXTENSION_MINDS_API_KEY`` env var on the
  ``mngr latchkey forward`` supervisor) so the proxy can inject
  ``Authorization: Bearer <key>`` on every forwarded request, and
* the desktop client's own ``/api/v1/...`` bearer-auth gate so it
  recognizes the key the proxy just injected.

This module deliberately does *not* hash the key on disk: there is no
multi-agent identification scheme to support anymore (the key is the
same for every caller), so the on-disk shape is just the plaintext
value at mode 0o600. The agent's identity, when relevant to a route,
comes from the URL path segment (e.g. ``/api/v1/agents/<agent_id>/...``),
which the latchkey gateway's per-host permissions file constrains to
agent ids that actually live on the caller's host.
"""

import hmac
import os
import secrets
from pathlib import Path
from typing import Final

from loguru import logger

# Filename under ``<data_dir>``. Plaintext is fine because the file is
# materialized at mode 0o600 and the directory is already user-owned;
# any attacker with read access here also has access to every other
# credential minds stores in the data dir.
_MINDS_API_KEY_FILENAME: Final[str] = "minds_api_key"

# Length in bytes of the random secret used as the API key. 32 bytes
# (256 bits) of entropy via ``secrets.token_urlsafe`` is comfortably
# more than the 122 bits of a UUID4 and avoids the dash-separated UUID
# shape (which is visually noisy when copy-pasted into shell env exports).
_API_KEY_BYTES: Final[int] = 32


def minds_api_key_path(data_dir: Path) -> Path:
    """Return the on-disk path of the central minds API key."""
    return data_dir / _MINDS_API_KEY_FILENAME


def generate_api_key() -> str:
    """Generate a fresh URL-safe random API key."""
    return secrets.token_urlsafe(_API_KEY_BYTES)


def load_or_create_minds_api_key(data_dir: Path) -> str:
    """Return the persisted minds API key, creating it on first call.

    Idempotent: subsequent calls return the same value from disk. The
    file is written atomically (write-to-tmp, fsync-equivalent rename)
    at mode 0o600 so a concurrent reader never sees a half-written
    file. A pre-existing file is left untouched so the key survives
    desktop-client restarts.
    """
    path = minds_api_key_path(data_dir)
    if path.is_file():
        existing = path.read_text().strip()
        if existing:
            return existing
        logger.warning("Found empty {} on disk; regenerating", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = generate_api_key()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(key)
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)
    logger.debug("Generated and persisted minds API key at {}", path)
    return key


def is_valid_minds_api_key(presented: str, expected: str) -> bool:
    """Constant-time comparison of a presented bearer token against the central key.

    Use this from request-handling code rather than ``==`` so a
    malicious caller cannot side-channel a guessing attack against
    the per-byte string comparison.
    """
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)
