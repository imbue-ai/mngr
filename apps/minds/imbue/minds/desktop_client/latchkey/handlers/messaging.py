"""``mngr message`` helper shared by the latchkey permission handlers.

Both sibling handlers in this package (:mod:`.predefined` and
:mod:`.file_sharing`) notify the waiting agent on resolution by
running ``mngr message`` through a :class:`~imbue.minds.utils.mngr_caller.MngrCaller`.
The class lives alongside them rather than inside either handler module
so neither sibling has to import from the other.
"""

import json
import threading
import time
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

# A nudge can fail transiently (e.g. a forkserver child that dies before
# delivering). Retry within this wall-clock budget before giving up so an
# approval does not silently leave the agent blocked.
_DEFAULT_DELIVERY_RETRY_BUDGET_SECONDS: Final[float] = 30.0
_DEFAULT_DELIVERY_RETRY_WAIT_SECONDS: Final[float] = 2.0


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

    A failed nudge is retried within a bounded budget and, if it still does not
    land, logged but never raised: the response event has already been written,
    so an undelivered nudge is recoverable (the agent will eventually wake up on
    its own).

    Each ``mngr message`` runs through a :class:`MngrCaller`, which executes the
    CLI in a child forked from a pre-warmed forkserver rather than spawning a
    brand-new subprocess -- avoiding the multi-second interpreter+import startup
    cost. Production passes the shared, pre-warmed singleton; tests inject a
    recording double.
    """

    mngr_caller: MngrCaller = Field(
        default_factory=get_default_mngr_caller,
        description="Forkserver-backed in-app ``mngr`` CLI caller.",
    )
    concurrency_group: ConcurrencyGroup = Field(
        description="App concurrency group on which :meth:`send` dispatches the (non-blocking) delivery thread.",
    )
    delivery_retry_budget_seconds: float = Field(
        default=_DEFAULT_DELIVERY_RETRY_BUDGET_SECONDS,
        description="Total wall-clock budget for retrying an undelivered nudge on the background thread.",
    )
    delivery_retry_wait_seconds: float = Field(
        default=_DEFAULT_DELIVERY_RETRY_WAIT_SECONDS,
        description="Wait between delivery retry attempts.",
    )

    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    def send(self, agent_id: AgentId, text: str) -> None:
        """Fire-and-forget nudge: dispatch delivery (verify-and-retry) without blocking the caller.

        Runs on a thread tracked by :attr:`concurrency_group` and never raises.
        The background work retries until the target confirms receipt (a
        ``message_sent`` event, see :meth:`deliver`) or
        :attr:`delivery_retry_budget_seconds` is exhausted, so a transient
        failure -- e.g. a forkserver child that dies before delivering -- no
        longer silently leaves the agent blocked. Failures are logged, not raised.
        """
        self.concurrency_group.start_new_thread(
            self._deliver_with_retries,
            args=(str(agent_id), text),
            name="mngr-message-send",
            is_checked=False,
            on_failure=lambda exc: logger.opt(exception=True).error(
                "mngr message send to agent {} failed: {}", agent_id, exc
            ),
        )

    def _deliver_with_retries(self, target: str, text: str) -> bool:
        """Retry :meth:`deliver` until ``target`` confirms receipt or the budget runs out.

        Returns whether the message was ultimately delivered. Delivery is judged
        by the ``message_sent`` event (not the exit code), so this recovers both
        hard failures (a child that crashes without delivering) and the
        "agent not matched yet" case. At least one attempt always runs; on
        exhaustion it logs an error -- the response event is already persisted, so
        the agent can still wake on its own, but the dropped nudge is now visible
        rather than silent.
        """
        deadline = time.monotonic() + self.delivery_retry_budget_seconds
        attempt = 0
        delivered = False
        while not delivered:
            attempt += 1
            delivered = self.deliver(target, text)
            if delivered or time.monotonic() >= deadline:
                break
            # ``Event().wait`` rather than ``time.sleep`` so the wait is
            # interruptible and consistent with the sibling onboarding poller.
            threading.Event().wait(timeout=self.delivery_retry_wait_seconds)
        if delivered:
            if attempt > 1:
                logger.info("mngr message to target {} delivered on attempt {}", target, attempt)
            return True
        logger.error(
            "mngr message to target {} not delivered after {} attempt(s) within {:.0f}s; "
            "the agent may stay blocked until it wakes on its own",
            target,
            attempt,
            self.delivery_retry_budget_seconds,
        )
        return False

    def deliver(self, target: str, text: str) -> bool:
        """Send a message and return whether the TARGET agent actually received it.

        Delivery is judged from the structured ``--format jsonl`` output (a
        ``message_sent`` event) rather than the process exit code. ``mngr
        message`` exits 0 both when it delivers and when no agent matches the
        target, so a caller that retries until the agent exists (see
        :meth:`_deliver_with_retries`) must inspect the output, not the exit code.
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
