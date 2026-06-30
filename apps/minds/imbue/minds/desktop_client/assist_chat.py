"""Spawn an in-workspace ``/assist`` chat to help diagnose a problem.

When the user picks "have an agent help" in a loaded workspace, the desktop app
runs ``mngr create`` *inside* that workspace's container (via ``mngr exec``) to
spawn a new chat agent seeded with ``/assist <description>``. Running the create
inside the container is what lets it resolve the FCT's ``chat`` create-template
and land in the right work dir -- exactly the way the workspace's own UI creates
chats -- while keeping the coupling at the mngr CLI level (no call into the
system-interface HTTP API). The new chat is tagged with the ``assist`` label so
the system interface auto-opens its tab.

``mngr exec`` runs its COMMAND argument through a shell on the host, so the inner
``mngr create`` is assembled as a single shell string with every token quoted
(``shlex.join``); the agent-supplied description therefore cannot break out of
the ``--message`` argument.
"""

import secrets
import shlex
from typing import Final

from loguru import logger

from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# Label marking a chat as a get-help ``/assist`` session. The system interface
# auto-opens the tab for any newly-discovered agent carrying it.
ASSIST_CHAT_LABEL: Final[str] = "assist"

# The mngr binary to invoke *inside* the container. Bare ``mngr`` resolves on the
# container's PATH (set up by ``mngr exec``'s source-env prefix); the outer
# binary path the desktop app uses would not exist inside the container.
_CONTAINER_MNGR_BINARY: Final[str] = "mngr"

# Generous timeout: the inner ``mngr create`` spawns a fresh chat agent (tmux
# window, claude process) on the existing host. No host provisioning or git
# transfer happens, but it is still slower than a plain message.
_ASSIST_SPAWN_TIMEOUT_SECONDS: Final[float] = 120.0


def generate_assist_chat_name() -> str:
    """Return a unique-enough chat name for an /assist session (``assist-<hex>``)."""
    return f"assist-{secrets.token_hex(3)}"


def build_assist_chat_mngr_args(
    workspace_agent_id: AgentId,
    workspace_name: str | None,
    description: str,
    chat_name: str,
) -> list[str]:
    """Build the ``mngr`` CLI args (sans the leading ``mngr``) that spawn the /assist chat.

    Returns the argument vector for a :class:`MngrCaller`: an ``exec`` targeting the
    workspace agent by id (a bare id is a valid agent address), whose single COMMAND
    argument is an inner ``mngr create`` shell string. The inner string is built with
    ``shlex.join`` so the free-text ``--message`` value is safely quoted.
    """
    inner_parts = [
        _CONTAINER_MNGR_BINARY,
        "create",
        chat_name,
        "--template",
        "chat",
        "--transfer",
        "none",
        "--no-connect",
        "--label",
        f"{ASSIST_CHAT_LABEL}=true",
    ]
    # Group the chat with its workspace (the same ``workspace=<host_name>`` label
    # the workspace's own agents carry) when we can resolve the name.
    if workspace_name:
        inner_parts += ["--label", f"workspace={workspace_name}"]
    inner_parts += ["--message", f"/assist {description}"]
    inner_command = shlex.join(inner_parts)
    return ["exec", "--agent", str(workspace_agent_id), inner_command]


def spawn_assist_chat(
    mngr_caller: MngrCaller,
    workspace_agent_id: AgentId,
    workspace_name: str | None,
    description: str,
    chat_name: str | None = None,
) -> bool:
    """Spawn the /assist chat and wait for ``mngr create`` to finish; return whether it succeeded.

    Runs synchronously (it blocks for the duration of the inner ``mngr create``, which spawns the
    agent + its claude process) so the caller -- the ``/help/assist`` route -- can hold the get-help
    modal in its "starting..." state until the chat actually exists, rather than dismissing into a
    blank several-second gap before the tab appears. The desktop server is a 50-thread WSGI pool, so
    blocking one request thread for the create does not stall other requests or the SSE streams. A
    non-zero exit is logged and surfaced to the caller as ``False`` so the modal can show an error.
    """
    resolved_chat_name = chat_name if chat_name is not None else generate_assist_chat_name()
    args = build_assist_chat_mngr_args(
        workspace_agent_id=workspace_agent_id,
        workspace_name=workspace_name,
        description=description,
        chat_name=resolved_chat_name,
    )
    result = mngr_caller.call(args, timeout=_ASSIST_SPAWN_TIMEOUT_SECONDS)
    if result.returncode != 0:
        logger.error(
            "Spawning /assist chat in workspace {} exited {}: {}",
            workspace_agent_id,
            result.returncode,
            result.stderr.strip(),
        )
        return False
    return True
