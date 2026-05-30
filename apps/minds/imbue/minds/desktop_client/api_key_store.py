"""Generation + constant-time verification of the central ``MINDS_API_KEY``.

There is one ``MINDS_API_KEY`` per ``minds run``, freshly generated in
memory on every startup and handed to:

* the latchkey gateway's bundled ``minds-api-proxy`` extension (via the
  ``LATCHKEY_EXTENSION_MINDS_API_KEY`` env var on the
  ``mngr latchkey forward`` supervisor, which ``minds run`` always
  restarts) so the proxy can inject ``Authorization: Bearer <key>`` on
  every forwarded request, and
* the desktop client's own ``/api/v1/...`` bearer-auth gate (via
  ``app.state.minds_api_key``) so it recognizes the key the proxy just
  injected.

The key is *not* persisted: the supervisor is restarted on every minds
startup and gets the current value in its env, the bare-origin server
sees the same in-memory value, and nothing else in the monorepo reads
the key from disk. Letting it rotate per-startup removes a long-lived
secret from the filesystem and shrinks the window of a compromised key
to a single minds session.

The agent's identity, when relevant to a route, comes from the URL
path segment (e.g. ``/api/v1/agents/<agent_id>/...``), which the
latchkey gateway's per-host permissions file constrains to agent ids
that actually live on the caller's host.
"""

import hmac
import secrets
from typing import Final

# Length in bytes of the random secret used as the API key. 32 bytes
# (256 bits) of entropy via ``secrets.token_urlsafe`` is comfortably
# more than the 122 bits of a UUID4 and avoids the dash-separated UUID
# shape (which is visually noisy when copy-pasted into shell env exports).
_API_KEY_BYTES: Final[int] = 32


def generate_api_key() -> str:
    """Generate a fresh URL-safe random API key."""
    return secrets.token_urlsafe(_API_KEY_BYTES)


def is_valid_minds_api_key(presented: str, expected: str) -> bool:
    """Constant-time comparison of a presented bearer token against the central key.

    Use this from request-handling code rather than ``==`` so a
    malicious caller cannot side-channel a guessing attack against
    the per-byte string comparison.
    """
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)
