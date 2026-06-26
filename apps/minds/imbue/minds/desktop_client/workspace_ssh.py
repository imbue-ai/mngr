"""Authorize temporary SSH access into a workspace, from the minds hub.

The "source workspace is still online" recovery route: a calling workspace
generates its own keypair and sends only its *public* key here. The hub appends
that key to the target workspace's ``authorized_keys`` (via ``mngr exec``),
tagged with the requester and a short expiry so it can be pruned later, and
returns the target's SSH connection info. The caller then connects directly with
its own private key and runs ordinary ``git`` / ``rsync`` / ``ssh``.

Private keys never move: the hub only ever handles public keys. Grants are
ephemeral -- each authorized key carries an ``expires=`` marker so that stale
grants can be pruned. ``prune_expired_grant_lines`` implements that pruning over
an ``authorized_keys`` body, and ``compose_pruned_authorized_keys`` combines it
with the new grant line in a single rewrite. The grant flow (see the route)
reads the target's ``authorized_keys`` back over ``mngr exec``, prunes expired
minds-owned lines, and writes the pruned body plus the new grant, so stale
grants never accumulate across repeated requests.

For a *remote* target (Modal / AWS / Vultr / imbue_cloud), the returned host is
reachable from anywhere, so the caller connects directly. A *local* target
(Docker / Lima) has no hub-resolvable external SSH endpoint, so brokering a
forwarding tunnel for that remote->local case is not yet implemented (see the
route).
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


def _validate_requester_workspace_id(requester_workspace_id: str) -> str:
    """Return the requester id, or raise on anything not safe in a single-line marker.

    The requester id is embedded verbatim into the marker comment of a
    single ``authorized_keys`` line, so -- exactly like the public key -- it
    must not contain whitespace (a newline would inject an extra
    ``authorized_keys`` line; any internal space/tab would break the
    space-delimited marker tokens). mngr ids are a single ``[A-Za-z0-9_-]``
    token, so rejecting whitespace never refuses a legitimate id.
    """
    if not requester_workspace_id:
        raise SshGrantError("requester_workspace_id is empty")
    if any(char.isspace() for char in requester_workspace_id):
        raise SshGrantError("requester_workspace_id must not contain whitespace")
    return requester_workspace_id


def build_authorized_keys_line(*, public_key: str, requester_workspace_id: str, expires_at: datetime) -> str:
    """Build the tagged ``authorized_keys`` line for a grant.

    Keeps only the key type + material (dropping any comment the caller's key
    carried) and appends our own marker comment carrying the requester id and
    expiry, so the line is unambiguously minds-owned and prunable.
    """
    validated = _validate_public_key(public_key)
    validated_requester = _validate_requester_workspace_id(requester_workspace_id)
    key_type, key_material = validated.split()[0], validated.split()[1]
    marker = f"{_GRANT_MARKER} {_REQUESTER_KEY}={validated_requester} {_EXPIRES_KEY}={expires_at.isoformat()}"
    return f"{key_type} {key_material} {marker}"


def _marker_token_value(line: str, token_key: str) -> str | None:
    """Return the value of the ``<token_key>=`` token in a minds-owned grant line, else None.

    Lines without our marker (keys the user added by hand) return None, as do
    minds-owned lines that simply lack the requested token. This is the single
    place that knows how grant marker tokens are encoded.
    """
    if _GRANT_MARKER not in line:
        return None
    prefix = f"{token_key}="
    for token in line.split():
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def _parse_grant_expiry(line: str) -> datetime | None:
    """Return the expiry encoded in a minds-owned authorized_keys line, else None.

    Lines without our marker (keys the user added by hand) return None and are
    never pruned. A marker with an unparseable *or* timezone-naive expiry is
    treated as expired (returns the epoch) so a corrupt grant doesn't linger
    forever and so the comparison against an aware ``now`` can never raise. The
    epoch sentinel is timezone-aware so it compares cleanly against an aware
    ``now``; minds only ever writes aware (``...+00:00``) expiries.
    """
    if _GRANT_MARKER not in line:
        return None
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    raw = _marker_token_value(line, _EXPIRES_KEY)
    if raw is None:
        return epoch
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return epoch
    return parsed if parsed.tzinfo is not None else epoch


def _grant_requester(line: str) -> str | None:
    """Return the requester id encoded in a minds-owned grant line, else None.

    Lines without our marker (keys the user added by hand) return None so they
    are never treated as belonging to any requester and thus never superseded.
    """
    return _marker_token_value(line, _REQUESTER_KEY)


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


def compose_pruned_authorized_keys(
    existing_content: str, new_authorized_line: str, *, requester_workspace_id: str, now: datetime
) -> str:
    """Return the full ``authorized_keys`` body to write back for a grant.

    Prunes expired minds-owned grants from the existing body and also drops any
    still-valid minds-owned grant belonging to ``requester_workspace_id`` (the
    new grant supersedes it, so a re-request *refreshes* rather than *stacks*),
    then appends the new grant line. Every user-managed key and every grant from
    a *different* requester is preserved verbatim. The result is newline-
    terminated so the file ends cleanly. This is the single source of truth for
    what a grant writes back, so the grant flow only has to read the current
    body, call this, and write the result -- neither expired grants nor
    duplicate same-requester grants can accumulate across repeated requests.
    """
    pruned = prune_expired_grant_lines(existing_content, now=now)
    # Drop any still-valid grant the same requester already holds; the new line
    # replaces it (refresh-not-stack). User keys (requester None) never match.
    kept = [line for line in pruned.splitlines() if _grant_requester(line) != requester_workspace_id]
    body = "\n".join(kept) + "\n" if kept else ""
    return f"{body}{new_authorized_line}\n"


class SshConnectionInfo(FrozenModel):
    """The connection info a caller needs to SSH into a target workspace."""

    user: str = Field(description="SSH username on the target host")
    host: str = Field(description="Reachable host/IP of the target")
    port: int = Field(description="SSH port on the target host")
    expires_at: datetime = Field(description="When the injected key grant expires (UTC)")
