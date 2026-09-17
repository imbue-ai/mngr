"""The resolution nudge shared by the latchkey permission handlers.

Every permission handler in this package notifies the waiting agent on
resolution through a :class:`~imbue.minds.utils.mngr_caller.MngrCaller` (the
shared epilogue in :mod:`.resolution` calls :meth:`MngrMessageSender.send`).
The request's ``agent_id`` is the CHAT the request belongs to (the workspace
template's chat-agent split, ``docs/system/blueprint/chat-agent-split/`` there):
a chat can move to a new agent, so the nudge goes to the chat app inside the
workspace, which delivers it to whichever agent the chat runs on now, with a
direct ``mngr message`` as the backoff for a workspace whose template predates
that script. The class lives alongside the handlers rather than inside any one
of them so no handler has to import from another.
"""

import json
import shlex
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.mngr.primitives import AgentId

_MNGR_MESSAGE_TIMEOUT_SECONDS: Final[float] = 30.0
# The chat app's send route blocks through the harness's paste-and-confirm, and the
# script retries a not-ready chat app for half a minute before giving up.
_MESSAGE_CHAT_TIMEOUT_SECONDS: Final[float] = 90.0

# The in-workspace script that messages a chat by id through the chat app; relative to the
# workspace repo root, which is the cwd ``mngr exec`` gives every command there.
MESSAGE_CHAT_SCRIPT: Final[str] = "system/scripts/message_chat.py"

# Backoff ramp for :meth:`MngrMessageSender.send`; after it, retries continue
# at the final interval until delivery or app shutdown, so a workspace that
# comes back hours later still hears its verdict without any bookkeeping
# surviving the process. The card does not depend on this message (it reads
# verdicts from the response event log via the shell), so a nudge lost to an
# app exit costs only the agent's early wake-up.
_SEND_RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0)


@pure
def format_resolution_notice(message: str, request_event_id: str, status: RequestStatus) -> str:
    """The agent-facing text for a resolved request: ``message`` plus a machine-readable tag.

    ``request_event_id`` is the gateway request id, reused verbatim as the request/
    response event id and echoed on the request's own tool-call result, so the chat
    harness pairs this notice with the right permission card by id instead of
    guessing from arrival order (which swaps verdicts on out-of-order resolutions).
    The verdict rides in the same tag so the harness never has to recognise the
    handler-authored English phrasing (a cross-repo coupling that has broken
    before; see ``message_display.py`` in the workspace template). Appended rather
    than folded in because ``message`` is also shown verbatim to the human user.
    """
    verdict = "granted" if status is RequestStatus.GRANTED else "denied"
    return f"{message} (resolution: {verdict}, request_id: {request_event_id})"


@pure
def stdout_reports_message_delivered(stdout: str) -> bool:
    """True if ``mngr message --format jsonl`` stdout reports a successful delivery.

    ``mngr message`` emits one ``{"event": "message_sent", "agent": ...}``
    JSONL line per agent it actually delivered to. Because the command is
    scoped by an include filter to a single target, the presence of any
    ``message_sent`` event means that target received the message.

    This is the source of truth for delivery -- the process exit code is
    not, because ``mngr message`` exits 0 both when it delivers AND when no
    agent matches the target (so exit code alone cannot distinguish
    "delivered" from "the agent does not exist yet").
    """
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        # mngr interleaves human-readable warnings on stdout; only attempt to
        # parse lines that look like a JSONL record (mirrors the ``mngr
        # create`` event sniff in ``agent_creator._CreateEventCapture``).
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "message_sent":
            return True
    return False


@pure
def message_chat_argv(exec_agent_id: str, chat_id: str, text: str) -> list[str]:
    """The ``mngr exec`` that messages a chat through its workspace's chat app.

    ``mngr exec`` runs the script on ``exec_agent_id``, an agent of the chat's workspace
    (the chat's newest member, which the backend resolver names for a chat id: a seeded
    chat's id is its seed's, not any agent's). The script runs from the workspace root,
    where it finds the chat app and posts to its send route by chat id; the chat app
    delivers to the chat's active agent, or holds the message while the chat moves to a
    new one.

    ``mngr exec`` takes every positional but the last as an agent and the last as ONE shell
    command string, so the script invocation is shell-quoted into a single argument; the
    notice text carries spaces and parentheses. ``--no-start``: a verdict for a stopped
    workspace waits for it to come up (the caller retries) rather than booting it.
    """
    return ["exec", exec_agent_id, shlex.join(["python3", MESSAGE_CHAT_SCRIPT, chat_id, "-m", text]), "--no-start"]


# What python prints when the workspace has no messaging script (a template from before
# it): ``python3: can't open file '<path>': [Errno 2] No such file or directory``. Only the
# first clause is matched: mngr's own stderr can carry the errno text from an unrelated
# provider warning (an unreachable Docker socket).
_SCRIPT_MISSING_MARKERS: Final[tuple[str, ...]] = ("can't open file",)


@pure
def is_message_chat_unavailable(stderr: str) -> bool:
    """Whether a failed ``mngr exec`` of the chat script failed for want of the script rather than by its verdict.

    ``mngr exec`` folds every failure into exit 1, so the script's own verdict (refused, or
    blocked on a dialog) and a missing script look alike by code; only the missing script has
    python's complaint on stderr. A refusal is never second-guessed with a direct send: while
    a chat moves to a new agent the chat app holds the message for it, and a send around the
    app would land on the agent the chat is leaving.
    """
    return any(marker in stderr for marker in _SCRIPT_MISSING_MARKERS)


class MngrMessageSender(MutableModel):
    """Delivers a resolution notice to the chat a permission request belongs to.

    The chat app inside the workspace is tried first (``message_chat_argv``), so a chat that
    moved to another agent still hears its verdict; ``mngr message <agent-id> <text>`` is the
    backoff for a workspace without the script.

    Failures are logged at warning level but never raised: the response
    event has already been written, so an undelivered nudge is recoverable
    (the agent will eventually wake up on its own).

    Each call runs through a :class:`MngrCaller`, which hands the CLI
    to a pre-warmed, single-use ``mngr`` process rather than spawning (and
    importing) a brand-new interpreter -- avoiding the multi-second
    interpreter+import startup cost. Production passes the shared, pre-warmed
    singleton; tests inject a recording double.
    """

    mngr_caller: MngrCaller = Field(
        default_factory=get_default_mngr_caller,
        description="Forkserver-backed in-app ``mngr`` CLI caller.",
    )
    concurrency_group: ConcurrencyGroup = Field(
        description="App concurrency group on which :meth:`send` dispatches the (non-blocking) delivery thread.",
    )
    retry_delays_seconds: tuple[float, ...] = Field(
        default=_SEND_RETRY_DELAYS_SECONDS,
        description=(
            "Backoff ramp between delivery attempts; the final entry repeats until delivery or "
            "shutdown. Injectable so tests avoid real waits."
        ),
    )

    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    def send(self, chat_id: AgentId, text: str, exec_agent_id: AgentId) -> None:
        """Dispatch the message to ``chat_id`` without blocking the caller, retrying until it lands.

        ``exec_agent_id`` is the agent the chat script runs on (see ``message_chat_argv``);
        for a chat that is its own first agent it is the chat id itself.

        The send runs on a thread tracked by :attr:`concurrency_group` and
        never raises -- failures are logged. Undelivered attempts retry on the
        :attr:`retry_delays_seconds` ramp and then at its final interval for as
        long as the app runs, so a resolution given while the agent is stopped
        reaches it whenever the workspace next comes up.
        """
        self.concurrency_group.start_new_thread(
            self._send_with_retries,
            args=(str(chat_id), text, str(exec_agent_id)),
            name="resolution-nudge-send",
            is_checked=False,
            on_failure=lambda exc: logger.opt(exception=True).error(
                "resolution nudge to chat {} failed: {}", chat_id, exc
            ),
        )

    def _send_with_retries(self, target: str, text: str, exec_agent_id: str) -> bool:
        """Deliver ``text`` to ``target``, retrying until delivery or shutdown.

        The between-attempt waits ride the concurrency group's shutdown
        event, so an app shutdown interrupts the backoff immediately. A nudge
        abandoned that way is not re-sent by a later run; the card still
        learns the verdict from the response event log, and the agent catches
        up the next time it is spoken to.
        """
        attempt_index = 0
        is_shutting_down = False
        while not is_shutting_down:
            if self.deliver(target, text, exec_agent_id):
                if attempt_index > 0:
                    logger.info("resolution nudge to target {} delivered after retry", target)
                return True
            # An empty schedule means single-attempt (tests use it to keep a
            # failing send to one call).
            if not self.retry_delays_seconds:
                logger.warning("resolution nudge to target {} was not delivered and retries are disabled", target)
                return False
            if attempt_index == len(self.retry_delays_seconds):
                logger.warning(
                    "resolution nudge to target {} is still undelivered after the backoff ramp; retrying "
                    "every {}s until it lands or the app exits",
                    target,
                    self.retry_delays_seconds[-1],
                )
            delay_index = min(attempt_index, len(self.retry_delays_seconds) - 1)
            is_shutting_down = self.concurrency_group.shutdown_event.wait(
                timeout=self.retry_delays_seconds[delay_index]
            )
            attempt_index += 1
        logger.info("resolution nudge retry to target {} abandoned: shutting down", target)
        return False

    def deliver(self, target: str, text: str, exec_agent_id: str) -> bool:
        """Deliver the notice to the chat ``target`` names and return whether it landed.

        The chat app is tried first: ``mngr exec`` on ``exec_agent_id`` (an agent of the
        chat's workspace) runs the workspace's messaging script, and exit 0 is the chat
        app's word that the message was delivered or queued. Only when the script is not
        there to run (an older workspace, whose chats are their own agents) does the direct
        ``mngr message`` to that agent run; any other failure is retried by the caller.
        """
        exec_result = self.mngr_caller.call(
            message_chat_argv(exec_agent_id, target, text), timeout=_MESSAGE_CHAT_TIMEOUT_SECONDS
        )
        if exec_result.returncode == 0:
            return True
        if not is_message_chat_unavailable(exec_result.stderr):
            logger.debug(
                "the chat app of target {} did not take the message (exit {}); stderr: {}",
                target,
                exec_result.returncode,
                exec_result.stderr.strip(),
            )
            return False
        logger.debug("target {} has no chat app script to message through; falling back to mngr message", target)
        return self._deliver_through_mngr_message(exec_agent_id, text)

    def _deliver_through_mngr_message(self, target: str, text: str) -> bool:
        """Send with ``mngr message`` and return whether the TARGET agent actually received it.

        ``target`` is matched by ``mngr message`` against agent ids and names,
        so a caller can address an agent by its host name before its canonical
        id is known. Delivery is judged from the structured ``--format jsonl``
        output (a ``message_sent`` event) rather than the process exit code:
        ``mngr message`` exits 0 both when it delivers AND when no agent
        matches the target, so a caller that retries until the agent exists
        must inspect the output.

        ``-m`` and ``--`` are required: ``mngr message`` treats every positional
        argument as an agent identifier (``nargs=-1``), so passing the text as a
        positional would be parsed as a second agent and the actual message
        content would be read from stdin (silently empty here).
        """
        result = self.mngr_caller.call(
            ["message", "--format", "jsonl", "-m", text, "--", target], timeout=_MNGR_MESSAGE_TIMEOUT_SECONDS
        )
        is_delivered = stdout_reports_message_delivered(result.stdout)
        if not is_delivered:
            logger.debug(
                "mngr message to target {} not yet delivered (exit {}); stderr: {}",
                target,
                result.returncode,
                result.stderr.strip(),
            )
        return is_delivered
