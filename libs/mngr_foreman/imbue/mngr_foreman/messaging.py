"""Send a message to one agent by name, matching ``mngr message`` semantics."""

from __future__ import annotations

from imbue.mngr_foreman.connection_pool import ConnectionPool
from imbue.mngr_foreman.connection_pool import send_via_pool


class MessageSendError(Exception):
    """Raised when a message could not be delivered to the target agent."""


def send_message_to_agent(pool: ConnectionPool, agent_name: str, message: str) -> None:
    """Deliver ``message`` to ``agent_name`` using the warm connection pool.

    The pool caches the resolved match list so repeat sends skip mngr's ~3s
    discovery and reuse the host's live SSH connection; only the tmux paste
    remains. Never auto-starts a stopped agent (``is_start_desired=False``) --
    foreman only ever shows and targets running agents. A blocking TUI dialog
    (unanswered permission / ``/login``) makes the send fail; that error string is
    surfaced verbatim so the UI can point at the terminal page. Raises
    :class:`MessageSendError` on any failure.
    """
    try:
        failed = send_via_pool(pool, agent_name, message)
    except LookupError as e:
        raise MessageSendError(str(e)) from e
    except Exception as e:  # noqa: BLE001 - resolution/discovery/connection failure
        raise MessageSendError(f"Could not reach agent {agent_name!r}: {e}") from e

    if failed:
        # One target: surface its error string directly (blocking TUI dialogs
        # land here -- the UI hints at the terminal page in that case).
        _name, error = failed[0]
        raise MessageSendError(error)
