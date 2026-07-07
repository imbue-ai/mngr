"""``mngr message`` helper shared by the latchkey permission handlers.

Both sibling handlers in this package (:mod:`.predefined` and
:mod:`.file_sharing`) notify the waiting agent on resolution by
running ``mngr message`` through a :class:`~imbue.minds.utils.mngr_caller.MngrCaller`.
The class lives alongside them rather than inside either handler module
so neither sibling has to import from the other.
"""

import json
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.mngr.primitives import AgentId

_MNGR_MESSAGE_TIMEOUT_SECONDS: Final[float] = 30.0


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


class MngrMessageSender(MutableModel):
    """Wrapper around ``mngr message <agent-id> <text>``.

    Failures are logged at warning level but never raised: the response
    event has already been written, so an undelivered nudge is recoverable
    (the agent will eventually wake up on its own).

    Each ``mngr message`` runs through a :class:`MngrCaller`, which hands the CLI
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

    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    def send(self, agent_id: AgentId, text: str) -> None:
        """Fire-and-forget nudge: dispatch the message without blocking the caller.

        The send runs on a thread tracked by :attr:`concurrency_group` and never raises -- failures are logged.
        """
        self.concurrency_group.start_new_thread(
            self.try_send,
            args=(str(agent_id), text),
            name="mngr-message-send",
            is_checked=False,
            on_failure=lambda exc: logger.opt(exception=True).error(
                "mngr message send to agent {} failed: {}", agent_id, exc
            ),
        )

    def try_send(self, target: str, text: str) -> bool:
        """Send a message to ``target`` (an agent id or name); return whether it succeeded.

        ``target`` is matched by ``mngr message`` against agent ids and
        names, so a caller can address an agent by its host name before its
        canonical id is known. Returns ``True`` when the invocation exits 0;
        logs the failure and returns ``False`` otherwise so pollers can retry.
        """
        # ``-m`` and ``--`` are required: ``mngr message`` treats every
        # positional argument as an agent identifier (``nargs=-1``), so passing
        # the text as a positional would be parsed as a second agent and the
        # actual message content would be read from stdin (silently empty here).
        result = self.mngr_caller.call(["message", "-m", text, "--", target], timeout=_MNGR_MESSAGE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            logger.error(
                "mngr message to target {} exited {}: {}",
                target,
                result.returncode,
                result.stderr.strip(),
            )
            return False
        return True

    def deliver(self, target: str, text: str) -> bool:
        """Send a message and return whether the TARGET agent actually received it.

        Unlike :meth:`try_send`, delivery is judged from the structured
        ``--format jsonl`` output (a ``message_sent`` event) rather than the
        process exit code. ``mngr message`` exits 0 both when it delivers and
        when no agent matches the target, so a caller that retries until the
        agent exists must inspect the output, not the exit code.
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
