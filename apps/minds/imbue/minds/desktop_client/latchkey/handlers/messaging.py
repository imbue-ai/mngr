"""``mngr message`` helper shared by the latchkey permission handlers.

Both sibling handlers in this package (:mod:`.predefined` and
:mod:`.file_sharing`) notify the waiting agent on resolution by
spawning ``mngr message``. The class lives alongside them rather than
inside either handler module so neither sibling has to import from
the other.
"""

from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.mngr.primitives import AgentId

_MNGR_MESSAGE_TIMEOUT_SECONDS: Final[float] = 30.0


class MngrMessageSender(MutableModel):
    """Wrapper around ``mngr message <agent-id> <text>``.

    Failures are logged at warning level but never raised: the response
    event has already been written, so an undelivered nudge is recoverable
    (the agent will eventually wake up on its own).
    """

    mngr_binary: str = Field(default=MNGR_BINARY, frozen=True, description="Path to mngr binary.")

    def send(self, agent_id: AgentId, text: str) -> None:
        cg = ConcurrencyGroup(name="mngr-message")
        with cg:
            result = cg.run_process_to_completion(
                # ``-m`` and ``--`` are required: ``mngr message`` treats every
                # positional argument as an agent identifier (``nargs=-1``), so
                # passing the text as a positional would be parsed as a second
                # agent and the actual message content would be read from
                # stdin (silently empty in this subprocess context).
                command=[self.mngr_binary, "message", "-m", text, "--", str(agent_id)],
                timeout=_MNGR_MESSAGE_TIMEOUT_SECONDS,
                is_checked_after=False,
            )
        if result.returncode != 0:
            logger.warning(
                "mngr message to agent {} exited {}: {}",
                agent_id,
                result.returncode,
                result.stderr.strip(),
            )
