"""Authorize temporary SSH access into a workspace, from the minds hub.

The "source workspace is still online" recovery route: a calling workspace
generates its own keypair and sends only its *public* key here. The hub appends
that key to the target workspace's ``authorized_keys`` (via ``mngr exec``),
tagged with the requester and a short expiry so it can be pruned later, and
returns the target's SSH connection info. The caller then connects directly with
its own private key and runs ordinary ``git`` / ``rsync`` / ``ssh``.

Private keys never move: the hub only ever handles public keys. Grants are
ephemeral -- each authorized key carries an ``expires=`` marker, and stale keys
are pruned (on the next grant for that target, and -- by a future caller --
at minds startup).

For a *remote* target (Modal / AWS / Vultr / imbue_cloud), the returned host is
reachable from anywhere, so the caller connects directly. A *local* target
(Docker / Lima) is not reachable from a remote caller; brokering a forwarding
tunnel for that remote->local case is not yet implemented (see the route).
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# Default lifetime of an authorized key grant. The plan caps this at ~1 day; a
# grant is meant to cover a single "pull my changes over" session, not linger.
DEFAULT_SSH_GRANT_TTL: timedelta = timedelta(hours=24)

# Marker that tags every key minds injects, so we own exactly the lines we
# wrote and can prune them without touching keys the user added by hand. The
# comment carries the requesting workspace and the expiry (UTC ISO 8601).
_GRANT_MARKER: str = "minds-ssh-grant"
_REQUESTER_KEY: str = "requester"
_EXPIRES_KEY: str = "expires"


class SshGrantError(ValueError):
    """Raised when an SSH grant request is malformed (e.g. a bad public key)."""


def _validate_public_key(public_key: str) -> str:
    """Return the single-line public key, or raise on anything that isn't one.

    An ``authorized_keys`` entry is a single line; reject embedded newlines (a
    crafted value must not be able to inject extra authorized_keys lines) and
    require the standard ``<type> <base64>`` shape.
    """
    stripped = public_key.strip()
    if not stripped:
        raise SshGrantError("public_key is empty")
    if "\n" in stripped or "\r" in stripped:
        raise SshGrantError("public_key must be a single line")
    parts = stripped.split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise SshGrantError("public_key is not a recognized OpenSSH public key")
    return stripped


def build_authorized_keys_line(*, public_key: str, requester_workspace_id: str, expires_at: datetime) -> str:
    """Build the tagged ``authorized_keys`` line for a grant.

    Keeps only the key type + material (dropping any comment the caller's key
    carried) and appends our own marker comment carrying the requester id and
    expiry, so the line is unambiguously minds-owned and prunable.
    """
    validated = _validate_public_key(public_key)
    key_type, key_material = validated.split()[0], validated.split()[1]
    marker = f"{_GRANT_MARKER} {_REQUESTER_KEY}={requester_workspace_id} {_EXPIRES_KEY}={expires_at.isoformat()}"
    return f"{key_type} {key_material} {marker}"


def _parse_grant_expiry(line: str) -> datetime | None:
    """Return the expiry encoded in a minds-owned authorized_keys line, else None.

    Lines without our marker (keys the user added by hand) return None and are
    never pruned. A marker with an unparseable expiry is treated as expired
    (returns the epoch) so a corrupt grant doesn't linger forever. The epoch
    sentinel is timezone-aware so it compares cleanly against an aware ``now``.
    """
    if _GRANT_MARKER not in line:
        return None
    for token in line.split():
        if token.startswith(f"{_EXPIRES_KEY}="):
            raw = token[len(_EXPIRES_KEY) + 1 :]
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)


def prune_expired_grant_lines(authorized_keys_content: str, *, now: datetime) -> str:
    """Drop minds-owned grant lines whose expiry has passed; keep everything else.

    Non-minds lines (no marker) and unexpired grants are preserved verbatim,
    including blank/comment lines, so we never disturb keys the user manages.
    """
    kept: list[str] = []
    for line in authorized_keys_content.splitlines():
        expiry = _parse_grant_expiry(line)
        if expiry is not None and expiry <= now:
            continue
        kept.append(line)
    # Preserve a trailing newline iff the input had non-empty content.
    return "\n".join(kept) + "\n" if kept else ""


class SshConnectionInfo(FrozenModel):
    """The connection info a caller needs to SSH into a target workspace."""

    user: str = Field(description="SSH username on the target host")
    host: str = Field(description="Reachable host/IP of the target")
    port: int = Field(description="SSH port on the target host")
    expires_at: datetime = Field(description="When the injected key grant expires (UTC)")
