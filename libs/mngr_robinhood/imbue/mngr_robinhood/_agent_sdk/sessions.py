"""mngr-backed implementation of the documented session functions.

A "session" is a ``robinhood-``-prefixed mngr claude agent. The SDK ``session_id`` is claude's
native session UUID (read from the agent's ``claude_session_id`` state file), and the functions
here are keyed by ``directory`` (the agent's ``cwd``):

* ``list_sessions``    -- enumerate ``robinhood-`` agents for that directory (newest first).
* ``get_session_info`` -- read one session's ``SDKSessionInfo`` (or ``None`` if unknown).
* ``get_session_messages`` -- read the persisted transcript as ``SessionMessage`` objects.
* ``rename_session``   -- set a custom title (mngr agent rename).
* ``tag_session``      -- set/clear a tag (mngr agent label).

Status: signatures, return/raise contracts (``None`` / ``[]`` for unknown ids,
``FileNotFoundError`` for mutating an unknown id), and paging semantics are final; the bodies
that read mngr's live agent list / transcript are built out and verified in the next phase.
"""

from claude_agent_sdk import SDKSessionInfo
from claude_agent_sdk import SessionMessage

from imbue.mngr_robinhood._agent_sdk.client import AgentSdkNotImplementedError


def list_sessions(
    directory: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    include_worktrees: bool = True,
) -> list[SDKSessionInfo]:
    """List sessions (``robinhood-`` mngr agents) for ``directory``, most-recent first.

    Intended wiring: ``list_agents`` filtered to ``robinhood-`` agents whose ``cwd`` matches
    ``directory``, mapped to ``SDKSessionInfo`` (session id from the agent's
    ``claude_session_id`` state file; ``custom_title`` from the agent name/label; ``tag`` from
    an agent label; ``first_prompt`` / ``last_modified`` / ``cwd`` from the transcript and agent
    metadata), then sorted by ``last_modified`` desc and sliced by ``offset`` / ``limit``.
    """
    raise AgentSdkNotImplementedError("list_sessions live mngr wiring is implemented in the next phase.")


def get_session_info(session_id: str, directory: str | None = None) -> SDKSessionInfo | None:
    """Return one session's info, or ``None`` if no matching session exists in ``directory``."""
    raise AgentSdkNotImplementedError("get_session_info live mngr wiring is implemented in the next phase.")


def get_session_messages(
    session_id: str,
    directory: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[SessionMessage]:
    """Return the session's persisted transcript as ``SessionMessage`` objects (``[]`` if unknown).

    Intended wiring: locate the agent for ``session_id`` in ``directory``, read its native
    session JSONL transcript, map each user/assistant line to a ``SessionMessage`` (carrying the
    raw message payload), then apply ``offset`` / ``limit``.
    """
    raise AgentSdkNotImplementedError("get_session_messages live mngr wiring is implemented in the next phase.")


def rename_session(session_id: str, title: str, directory: str | None = None) -> None:
    """Set a session's custom title (mngr agent rename). Raises ``FileNotFoundError`` if unknown."""
    raise AgentSdkNotImplementedError("rename_session live mngr wiring is implemented in the next phase.")


def tag_session(session_id: str, tag: str | None, directory: str | None = None) -> None:
    """Set or clear a session's tag (mngr agent label). Raises ``FileNotFoundError`` if unknown."""
    raise AgentSdkNotImplementedError("tag_session live mngr wiring is implemented in the next phase.")
