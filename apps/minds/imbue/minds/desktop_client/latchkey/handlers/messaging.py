"""The resolution nudge shared by the latchkey permission handlers.

Every permission handler in this package notifies the waiting agent on
resolution through a :class:`~imbue.minds.utils.mngr_caller.MngrCaller` (the
shared epilogue in :mod:`.resolution` calls :meth:`MngrMessageSender.send`).
The request's ``agent_id`` is the CHAT the request belongs to (the workspace
template's chat-agent split, ``docs/system/blueprint/chat-agent-split/`` there):
a chat can move to a new agent, so the nudge goes to the chat app inside the
workspace (:func:`~imbue.minds.desktop_client.chat_app.ask_chat_app`, the same
path the app creates chats through), which delivers it to whichever agent the
chat runs on now. The backoff for a workspace whose template predates that
script is an ``mngr message`` run *inside* the workspace too, in the same
``mngr exec`` right behind the script: sending from the laptop works only for harnesses
whose plugin has a remote send path, and codex's has none (it reaches its
app-server over a unix socket that resolves inside the container), while inside
the workspace every agent's host is local. The class lives alongside the
handlers rather than inside any one of them so no handler has to import from
another.
"""

from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.chat_app import ChatAppVerdict
from imbue.minds.desktop_client.chat_app import ask_chat_app
from imbue.minds.desktop_client.in_workspace_mngr import build_in_workspace_mngr_command
from imbue.minds.desktop_client.in_workspace_mngr import jsonl_events
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.mngr_command import mngr_failure_verdict
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.primitives import AgentId

# Ceiling for one delivery attempt, either way. Generous because an exec pays the outer
# mngr's provider discovery, the hop into the container, and a python or mngr start
# inside it before the message moves; the chat app's send route then blocks through the
# harness's paste-and-confirm, and the script retries a not-ready chat app for half a
# minute. The send runs on a background thread behind a retry ramp, so a high ceiling
# only delays a retry.
_DELIVER_TIMEOUT_SECONDS: Final[float] = 90.0

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

    This is the source of truth for delivery -- neither process's exit code is,
    because ``mngr message`` exits 0 both when it delivers AND when no agent
    matches the target (so exit code alone cannot distinguish "delivered" from
    "the agent does not exist yet").
    """
    return any(event.get("event") == "message_sent" for event in jsonl_events(stdout))


@pure
def message_failure_reason(stdout: str) -> str:
    """Why ``mngr message --format jsonl`` did not deliver, in its own words; '' if it said nothing.

    ``mngr message`` reports each failure as a
    ``{"event": "message_error", "agent": ..., "error": ...}`` line. That reason
    is the only account of *what* went wrong: the exit code says only that
    something did, and mngr's stderr carries provider-discovery chatter that
    reads like a cause but is not.
    """
    return "; ".join(
        str(event["error"])
        for event in jsonl_events(stdout)
        if event.get("event") == "message_error" and "error" in event
    )


@pure
def build_inner_mngr_message_command(target_address: str, text: str) -> str:
    """The in-workspace shell string that runs ``mngr message`` to ``target_address``'s agent.

    The inner ``mngr message`` names the bare agent: a host the address pins is the laptop's
    view of it, and inside the workspace that host is local.

    ``-m`` and ``--`` are required: ``mngr message`` treats every positional argument as an
    agent identifier (``nargs=-1``), so passing the text as a positional would be parsed as
    a second agent and the actual message content would be read from stdin (silently empty
    here).
    """
    inner_target = str(parse_agent_address(target_address).agent)
    return build_in_workspace_mngr_command(["message", "--format", "jsonl", "-m", text, "--", inner_target])


class MngrMessageSender(MutableModel):
    """Delivers a resolution notice to the chat a permission request belongs to.

    The chat app inside the workspace is tried first (:func:`ask_chat_app`), so a chat that
    moved to another agent still hears its verdict; an ``mngr message`` inside the same
    workspace (:func:`build_inner_mngr_message_command`), run right behind the script in the
    same exec, is the backoff for a workspace without the script.

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

    def send(self, chat_id: AgentId, text: str, exec_agent_address: str) -> None:
        """Dispatch the message to ``chat_id`` without blocking the caller, retrying until it lands.

        ``exec_agent_address`` names the agent the chat script runs on: the chat's newest member,
        which the backend resolver names for a chat id (a seeded chat's id is its seed's, not
        any agent's). For a chat that is its own first agent it names the chat id itself.

        The send runs on a thread tracked by :attr:`concurrency_group` and
        never raises -- failures are logged. Undelivered attempts retry on the
        :attr:`retry_delays_seconds` ramp and then at its final interval for as
        long as the app runs, so a resolution given while the agent is stopped
        reaches it whenever the workspace next comes up.
        """
        self.concurrency_group.start_new_thread(
            self._send_with_retries,
            args=(str(chat_id), text, exec_agent_address),
            name="resolution-nudge-send",
            is_checked=False,
            on_failure=lambda exc: logger.opt(exception=True).error(
                "resolution nudge to chat {} failed: {}", chat_id, exc
            ),
        )

    def _send_with_retries(self, target: str, text: str, exec_agent_address: str) -> bool:
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
            if self.deliver(target, text, exec_agent_address):
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

    def deliver(self, target: str, text: str, exec_agent_address: str) -> bool:
        """Deliver the notice to the chat ``target`` names and return whether it landed.

        The chat app's word is final unless it gave none: a notice delivered behind a dialog
        is in the pane already, and a refusal is never second-guessed with a send around the
        chat app, since while a chat moves to a new agent the chat app holds the message for
        it and a direct send would land on the agent the chat is leaving; the caller retries
        instead, as it does a workspace the exec never reached. Only a template from before
        the script, whose chats are their own agents, gets the in-workspace ``mngr message``,
        which runs in the same exec. A stopped workspace is not booted for a notice; the caller
        waits for it to come up.

        The backoff's delivery is judged from its structured ``--format jsonl`` output (a
        ``message_sent`` event) rather than its exit status: ``mngr message`` exits 0 both when
        it delivers AND when no agent matches the target, so a caller that retries until the
        agent exists must inspect the output.
        """
        answer = ask_chat_app(
            self.mngr_caller,
            exec_agent_address,
            [target, "-m", text],
            fallback_command=build_inner_mngr_message_command(exec_agent_address, text),
            timeout=_DELIVER_TIMEOUT_SECONDS,
        )
        if answer.verdict is ChatAppVerdict.DELIVERED_BEHIND_DIALOG:
            logger.info("resolution nudge to target {} landed behind a dialog: {}", target, answer.detail)
        if answer.is_delivered:
            return True
        if answer.verdict is ChatAppVerdict.NO_VERDICT:
            logger.warning(
                "target {} gave no verdict on the nudge through its chat app ({}); fell back to mngr message",
                target,
                answer.detail,
            )
            fallback = answer.fallback
            if fallback is None:
                logger.debug(
                    "mngr message to target {} never reported back: {}", exec_agent_address, answer.log_detail
                )
                return False
            if stdout_reports_message_delivered(fallback.stdout):
                return True
            logger.debug(
                "mngr message to target {} not yet delivered (exit {}): {}",
                exec_agent_address,
                fallback.exit_code,
                message_failure_reason(fallback.stdout) or mngr_failure_verdict(fallback.stderr),
            )
            return False
        logger.debug(
            "the chat app of target {} did not take the message (script exit {}, exec exit {}): {}",
            target,
            answer.script_exit_code,
            answer.exec_returncode,
            answer.log_detail,
        )
        return False
