"""Automatically restart agents whose backends are unavailable.

When the forwarding server receives a request for an agent whose backend
URL is unknown (e.g. the tmux session was killed), this module runs
``mngr start <agent-id>`` in a background thread to restore the session.

Restart attempts are debounced per agent: once a restart is triggered,
no further attempts are made for that agent until the cooldown expires.
"""

import threading
import time
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.mngr.primitives import AgentId

_DEFAULT_COOLDOWN_SECONDS: Final[float] = 30.0


class AgentRestarter(MutableModel):
    """Restarts agents whose backends are unavailable by running ``mngr start``.

    Thread-safe: all state access is guarded by an internal lock.
    Restart attempts are debounced per agent with a configurable cooldown.
    """

    mngr_binary: str = Field(default=MNGR_BINARY, frozen=True, description="Path to mngr binary")
    cooldown_seconds: float = Field(
        default=_DEFAULT_COOLDOWN_SECONDS,
        frozen=True,
        description="Minimum seconds between restart attempts for the same agent",
    )

    _last_attempt_by_agent: dict[str, float] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def try_restart(self, agent_id: AgentId) -> None:
        """Attempt to restart an agent if not recently attempted.

        If a restart was already attempted within the cooldown period,
        this call is a no-op. Otherwise, starts ``mngr start <agent-id>``
        in a background thread.
        """
        aid_str = str(agent_id)
        now = time.monotonic()

        with self._lock:
            last_attempt = self._last_attempt_by_agent.get(aid_str, 0.0)
            if now - last_attempt < self.cooldown_seconds:
                return
            self._last_attempt_by_agent[aid_str] = now

        thread = threading.Thread(
            target=self._run_start,
            args=(agent_id,),
            daemon=True,
            name="agent-restarter-{}".format(aid_str),
        )
        thread.start()

    def _run_start(self, agent_id: AgentId) -> None:
        """Run ``mngr start <agent-id>`` synchronously in a background thread."""
        logger.info("Attempting to restart agent {} via mngr start", agent_id)
        cg = ConcurrencyGroup(name="agent-restart-{}".format(agent_id))
        with cg:
            result = cg.run_process_to_completion(
                command=[self.mngr_binary, "start", str(agent_id)],
                is_checked_after=False,
            )
        if result.returncode == 0:
            logger.info("Successfully restarted agent {}", agent_id)
        else:
            stderr = result.stderr.strip() if result.stderr else ""
            stdout = result.stdout.strip() if result.stdout else ""
            output = stderr or stdout
            logger.warning("Failed to restart agent {} (exit code {}): {}", agent_id, result.returncode, output)
