"""The loop that runs scheduled updates inside the configured window, and the rules for when it declines to.

Opportunistic: a skipped window leaves the intent armed and records why, while a real
failure, or an up-to-date workspace whose intent named no version of its own, disarms it.
"""

import threading
from collections.abc import Callable
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleRecord
from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleStore
from imbue.minds.desktop_client.update_status import UpdateSkipReason
from imbue.minds.errors import MindError
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostState

_TICK_INTERVAL_SECONDS: Final[float] = 300.0


def is_within_update_window(now: datetime, window: tuple[int, int]) -> bool:
    """Whether ``now`` (local) falls in the ``[start, end)`` hour window; ``(23, 3)`` wraps midnight."""
    start_hour, end_hour = window
    if start_hour < end_hour:
        return start_hour <= now.hour < end_hour
    return now.hour >= start_hour or now.hour < end_hour


def window_start_date(now: datetime, window: tuple[int, int]) -> date:
    """The local date the window containing ``now`` opened on (what "one attempt per window" counts against)."""
    start_hour, end_hour = window
    if start_hour > end_hour and now.hour < end_hour:
        return (now - timedelta(days=1)).date()
    return now.date()


class ScheduledRunConditions(FrozenModel):
    """The facts one scheduled attempt is decided on, gathered once per workspace."""

    is_offered: bool = Field(
        description="Whether a run could still sensibly happen here (out of date, or a version we could not read)"
    )
    is_run_in_flight: bool = Field(description="Whether an update is already running there")
    is_reachable: bool = Field(description="Whether the workspace's host is known and startable")
    is_quiet: bool = Field(description="Whether no chats are currently running in it")


def decide_skip_reason(conditions: ScheduledRunConditions, *, is_target_named: bool) -> UpdateSkipReason | None:
    """The reason this attempt is declined, or None to run it. Non-problems are checked first.

    A named target skips the availability check, as the buttons that take one do: it is measured
    against this app's release ceiling, and a named ref is reached precisely when the ceiling
    does not offer it. Only the run itself can tell whether the workspace is already on it.
    """
    if not is_target_named and not conditions.is_offered:
        return UpdateSkipReason.ALREADY_UP_TO_DATE
    if conditions.is_run_in_flight:
        return UpdateSkipReason.UPDATE_IN_FLIGHT
    if not conditions.is_reachable:
        return UpdateSkipReason.WORKSPACE_UNREACHABLE
    if not conditions.is_quiet:
        return UpdateSkipReason.CHATS_RUNNING
    return None


class UpdateScheduler(MutableModel):
    """Runs armed intents inside the update window, one attempt per workspace per window."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    schedule_store: UpdateScheduleStore = Field(frozen=True, description="The armed intents.")
    read_update_window: Callable[[], tuple[int, int]] = Field(
        frozen=True, description="The configured local-hour window."
    )
    read_conditions: Callable[[AgentId], ScheduledRunConditions] = Field(
        frozen=True, description="Gathers one workspace's decision facts (one remote round trip)."
    )
    read_host_state: Callable[[AgentId], HostState | None] = Field(
        frozen=True, description="The workspace's host state before the run, so it can be put back."
    )
    dispatch: Callable[[AgentId, str], bool] = Field(
        frozen=True,
        description="Starts the workspace if needed and spawns the update chat, targeting the given ref ('' for the default).",
    )
    stop_workspace: Callable[[AgentId], None] = Field(
        frozen=True, description="Puts a workspace that was stopped before the run back to stopped."
    )
    now: Callable[[], datetime] = Field(
        default=datetime.now, frozen=True, description="Local wall clock; injected in tests."
    )
    tick_interval_seconds: float = Field(default=_TICK_INTERVAL_SECONDS, description="Seconds between clock checks.")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Only workspaces that were STOPPED when their run began; one the user had running is theirs.
    _restore_to_stopped: set[str] = PrivateAttr(default_factory=set)
    # Keyed by the window's opening date, so a multi-hour window is one attempt rather than one per tick.
    _attempted_window_by_agent: dict[str, date] = PrivateAttr(default_factory=dict)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _concurrency_group: ConcurrencyGroup | None = PrivateAttr(default=None)
    _is_started: bool = PrivateAttr(default=False)

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the window loop. Idempotent."""
        if self._is_started:
            return
        self._is_started = True
        self._concurrency_group = concurrency_group
        concurrency_group.start_new_thread(
            target=self._run_loop,
            name="update-scheduler",
            daemon=True,
            # The loop logs its own failures; a crash must not poison the root group.
            is_checked=False,
        )

    def stop(self) -> None:
        """Stop the window loop (it exits at its next tick)."""
        self._stop_event.set()

    def run_now(self, agent_id: AgentId) -> UpdateSkipReason | None:
        """Attempt one workspace immediately under the schedule's gate; returns the skip reason, or None if dispatched.

        An armed intent's target is kept: "now" changes when its run happens, not which version it
        was pointed at. It is read before the gate, which turns on whether there is one.
        """
        armed = self.schedule_store.read(agent_id)
        target_ref = armed.target_ref if armed is not None else ""
        skip_reason = decide_skip_reason(self.read_conditions(agent_id), is_target_named=bool(target_ref))
        if skip_reason is not None:
            return skip_reason
        return self._dispatch_one(agent_id, target_ref=target_ref)

    def run_window_pass(self) -> None:
        """One pass over every armed intent; a no-op outside the window."""
        now = self.now()
        window = self.read_update_window()
        if not is_within_update_window(now, window):
            return
        this_window = window_start_date(now, window)
        for record in self.schedule_store.list_records():
            with self._lock:
                if self._attempted_window_by_agent.get(record.agent_id) == this_window:
                    continue
                self._attempted_window_by_agent[record.agent_id] = this_window
            self._attempt_one(record)

    def note_run_finished(self, agent_id: AgentId, *, is_real_failure: bool) -> None:
        """Close out a scheduled run: restore the prior host state and disarm the intent.

        Disarmed however it ended: a failed run is not retried unwatched. ``is_real_failure``
        only affects logging.
        """
        aid_str = str(agent_id)
        with self._lock:
            is_restore_due = aid_str in self._restore_to_stopped
            self._restore_to_stopped.discard(aid_str)
        if is_restore_due:
            logger.info("Putting {} back to stopped after its scheduled update", agent_id)
            self.stop_workspace(agent_id)
        was_armed = self.schedule_store.cancel(agent_id)
        if was_armed and is_real_failure:
            logger.info("Cancelled the update schedule for {}: its run failed", agent_id)

    def _attempt_one(self, record: UpdateScheduleRecord) -> None:
        agent_id = AgentId(record.agent_id)
        conditions = self.read_conditions(agent_id)
        skip_reason = decide_skip_reason(conditions, is_target_named=bool(record.target_ref))
        if skip_reason is None:
            skip_reason = self._dispatch_one(agent_id, target_ref=record.target_ref)
        if skip_reason is None:
            return
        if skip_reason is UpdateSkipReason.ALREADY_UP_TO_DATE:
            # Re-arming would skip silently forever, with no scheduled badge left to cancel it from.
            logger.info("Cancelled the update schedule for {}: it is already up to date", agent_id)
            self.schedule_store.cancel(agent_id)
            return
        logger.info("Skipped the scheduled update for {}: {}", agent_id, skip_reason.value)
        self.schedule_store.record_skip(agent_id, skip_reason.value)

    def _dispatch_one(self, agent_id: AgentId, *, target_ref: str) -> UpdateSkipReason | None:
        # Read before the dispatch, which may start the host.
        if self.read_host_state(agent_id) is HostState.STOPPED:
            with self._lock:
                self._restore_to_stopped.add(str(agent_id))
        if not self.dispatch(agent_id, target_ref):
            with self._lock:
                self._restore_to_stopped.discard(str(agent_id))
            return UpdateSkipReason.DISPATCH_FAILED
        return None

    def _run_loop(self) -> None:
        concurrency_group = self._concurrency_group
        assert concurrency_group is not None, "the loop only runs after start() recorded its group"
        # Sleeps on the group's shutdown event so a quit does not wait out a tick.
        while not concurrency_group.shutdown_event.wait(timeout=self.tick_interval_seconds):
            if self._stop_event.is_set():
                break
            try:
                self.run_window_pass()
            except (MindError, MngrError, OSError, RuntimeError, ValueError) as e:
                # One bad intent must not silently end the loop for every other armed
                # workspace; anything outside this set is a bug and should kill it.
                logger.opt(exception=e).error("An update-scheduler window pass failed; continuing")
        logger.debug("Exited the update-scheduler loop")
