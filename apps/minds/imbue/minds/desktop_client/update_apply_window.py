"""The apply window: suppress health/recovery for an update's apply step, and only that step.

The apply's reveal takes the workspace's services down for longer than the stuck
threshold, so without this the tracker goes STUCK and unattended recovery restarts
a healthy machine mid-apply. The workspace's own ``run.json`` (``apply_phase``,
``apply_updated_at``, read over ``mngr exec``) is the authority for whether an
apply is under way and for how long the window lasts; the prepare phase, which
leaves the live workspace untouched, gets normal outage handling.
"""

import shlex
import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import auto
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import ProbeGracePurpose
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.update_status import IN_FLIGHT_ACTIVITIES
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import parse_update_run_status
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.errors import MindError
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState

# Written by ``update_self.py run-status``; relative to the work_dir ``mngr exec`` lands in.
RUN_STATUS_PATH: Final[str] = "data/.state/update-apply/run.json"

# Mirrors ``DEFAULT_RECOVER_GRACE_SECONDS`` in the template's ``update_apply_contract.py``: how long
# a *dead* apply may go without restamping before the workspace's own recovery rolls it back. This
# window is the harsher of the two, deliberately: the app reads only the run record, never the
# marker, so it has no pid to test and hands a machine back to recovery on the restamp age alone.
APPLY_GRACE_SECONDS: Final[float] = 600.0

# Fallback window when the record's ``apply_updated_at`` could not be read.
DEFAULT_APPLY_WINDOW_SECONDS: Final[float] = 360.0

_EXPIRY_PASS_INTERVAL_SECONDS: Final[float] = 10.0

# Low so an unreachable workspace does not hold the stuck edge's callback thread.
_RUN_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

# STOPPED is what an agent with no tmux session reports (a container restart leaves
# that behind), so it counts as ended alongside DONE. Anything unlisted reads as running.
_ENDED_AGENT_STATES: Final[frozenset[str]] = frozenset(
    {AgentLifecycleState.DONE.value, AgentLifecycleState.STOPPED.value}
)

# Public: a wire format tests standing in for a workspace must spell identically.
RUN_BEGIN_SENTINEL: Final[str] = "MNGR_UPDATE_RUN_BEGIN"
RUN_END_SENTINEL: Final[str] = "MNGR_UPDATE_RUN_END"
AGENTS_BEGIN_SENTINEL: Final[str] = "MNGR_UPDATE_AGENTS_BEGIN"
AGENTS_END_SENTINEL: Final[str] = "MNGR_UPDATE_AGENTS_END"
# Printed when the in-container ``mngr`` could not answer (an apply replaces it mid-run),
# so "could not look" is never mistaken for "no agents".
AGENTS_FAILED_SENTINEL: Final[str] = "MNGR_UPDATE_AGENTS_FAILED"


class UpdateAgentLiveness(UpperCaseStrEnum):
    """Whether an update run's chat agent is still there in the workspace."""

    ALIVE = auto()
    GONE = auto()
    UNKNOWN = auto()
    """The probe could not answer. Never read as GONE: that would unlock a live run's row."""


class UpdateRunProbe(FrozenModel):
    """One workspace's answer to "is an update actually still happening here?"."""

    is_probe_answered: bool = Field(
        description="Whether the probe ran at all; False means the workspace is unreachable"
    )
    run_status: UpdateRunStatus | None = Field(
        default=None, description="The parsed run-status record, None when absent or unreadable"
    )
    agent_liveness: UpdateAgentLiveness = Field(
        default=UpdateAgentLiveness.UNKNOWN, description="Whether the run's chat agent is still there"
    )
    is_agent_waiting: bool = Field(
        default=False,
        description="Whether the run reads as alive but idle: its chat agent reported WAITING, and so did any "
        "worker the record names (or that worker is gone). An agent whose state could not be read reads as "
        "running rather than waiting, on the same bias as the rest",
    )

    @property
    def is_apply_in_progress(self) -> bool:
        """Whether the record says an apply is landing, whichever run's record it is."""
        return self.run_status is not None and self.run_status.is_apply_in_progress

    @property
    def apply_updated_at(self) -> datetime | None:
        """When the apply last moved, for sizing the window; None when unreadable or no apply."""
        return self.run_status.apply_updated_at if self.run_status is not None else None

    @property
    def is_run_alive(self) -> bool:
        """Whether the evidence says a run is still going; unanswered and UNKNOWN count as alive."""
        if not self.is_probe_answered:
            return True
        return self.is_apply_in_progress or self.agent_liveness is not UpdateAgentLiveness.GONE


def build_update_run_probe_args(workspace_agent_id: AgentId) -> list[str]:
    """Build the ``mngr`` CLI args for the combined run-status + agent probe.

    One exec for both facts because this runs on the stuck-edge callback thread. The
    listing is unfiltered and matched app-side so a chat name never lands in a CEL filter.
    """
    inner_list = shlex.join(["mngr", "list", "--format", "{name}\t{state}"])
    # The bare `echo` keeps the end sentinel on its own line when the record has no trailing newline.
    script = (
        f"echo {RUN_BEGIN_SENTINEL}; cat {shlex.quote(RUN_STATUS_PATH)} 2>/dev/null; echo; echo {RUN_END_SENTINEL}; "
        f"echo {AGENTS_BEGIN_SENTINEL}; "
        f"{inner_list} 2>/dev/null || echo {AGENTS_FAILED_SENTINEL}; "
        f"echo {AGENTS_END_SENTINEL}"
    )
    return ["exec", "--agent", str(workspace_agent_id), script, "--no-start"]


def _section(lines: list[str], begin: str, end: str) -> list[str] | None:
    """The raw lines between two sentinel lines, or None when either is missing."""
    stripped = [line.strip() for line in lines]
    if begin not in stripped or end not in stripped:
        return None
    begin_index = stripped.index(begin)
    end_index = stripped.index(end)
    if end_index < begin_index:
        return None
    return lines[begin_index + 1 : end_index]


def _state_of(listing_lines: list[str], agent_name: str) -> str | None:
    """The listed lifecycle state of ``agent_name``, or None when it is not listed."""
    for line in listing_lines:
        name, _, state = line.partition("\t")
        if name.strip() == agent_name:
            return state.strip()
    return None


def _classify_agent_listing(
    listing_lines: list[str], chat_agent_name: str, worker_agent_name: str
) -> tuple[UpdateAgentLiveness, bool]:
    """The run's chat agent's liveness (and whether the run is WAITING) from the listing.

    With no chat name to look for the answer is UNKNOWN, not GONE: nothing here can be
    attributed to the run, and GONE would unlock a live run's row. An answered listing
    that does not carry the name is GONE, since an ended chat stays listed.

    An idle lead is only "waiting" when its named worker is not moving either: the lead
    sits idle for most of the run while a background worker does the merge.
    """
    stripped = [line.strip() for line in listing_lines if line.strip()]
    if AGENTS_FAILED_SENTINEL in stripped:
        return UpdateAgentLiveness.UNKNOWN, False
    if not chat_agent_name:
        return UpdateAgentLiveness.UNKNOWN, False
    chat_state = _state_of(stripped, chat_agent_name)
    if chat_state is None or chat_state in _ENDED_AGENT_STATES:
        return UpdateAgentLiveness.GONE, False
    if chat_state != AgentLifecycleState.WAITING.value:
        return UpdateAgentLiveness.ALIVE, False
    worker_state = _state_of(stripped, worker_agent_name) if worker_agent_name else None
    is_worker_moving = (
        worker_state is not None
        and worker_state not in _ENDED_AGENT_STATES
        and worker_state != AgentLifecycleState.WAITING.value
    )
    return UpdateAgentLiveness.ALIVE, not is_worker_moving


def parse_update_run_probe(stdout: str, chat_agent_name: str) -> UpdateRunProbe:
    """Classify the probe's output; missing run sentinels mean unanswered, not "no run"."""
    lines = stdout.splitlines()
    run_lines = _section(lines, RUN_BEGIN_SENTINEL, RUN_END_SENTINEL)
    if run_lines is None:
        return UpdateRunProbe(is_probe_answered=False)
    run_text = "\n".join(run_lines).strip()
    run_status = parse_update_run_status(run_text) if run_text else None
    # Only this run's own record may vouch for a worker.
    is_records_run = run_status is not None and bool(chat_agent_name) and run_status.chat_agent_name == chat_agent_name
    worker_agent_name = run_status.worker_agent_name if is_records_run and run_status is not None else ""
    agent_lines = _section(lines, AGENTS_BEGIN_SENTINEL, AGENTS_END_SENTINEL)
    if agent_lines is None:
        liveness, is_waiting = UpdateAgentLiveness.UNKNOWN, False
    else:
        liveness, is_waiting = _classify_agent_listing(agent_lines, chat_agent_name, worker_agent_name)
    return UpdateRunProbe(
        is_probe_answered=True,
        run_status=run_status,
        agent_liveness=liveness,
        is_agent_waiting=is_waiting,
    )


class UpdateApplyWindowManager(MutableModel):
    """Owns the apply window: when it is open, who it silences, and how it ends."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    tracker: SystemInterfaceHealthTracker = Field(frozen=True, description="Whose probe grace the window arms.")
    store: WorkspaceUpdateStateStore = Field(frozen=True, description="Where the applying/stalled activity lands.")
    mngr_caller: MngrCaller = Field(frozen=True, description="Runs the run-status + agent probe.")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Parent group for the expiry pass.")
    dispatch_restart: Callable[[AgentId], None] = Field(
        frozen=True,
        description="The hand-off for a window that expired with the machine still stuck.",
    )
    fallback_window_seconds: float = Field(
        default=DEFAULT_APPLY_WINDOW_SECONDS,
        description="How long one apply window stays open when the apply's restamp time could not be read.",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _deadline_by_agent: dict[str, float] = PrivateAttr(default_factory=dict)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _is_started: bool = PrivateAttr(default=False)

    def start(self) -> None:
        """Start the expiry pass strand. Idempotent."""
        if self._is_started:
            return
        self._is_started = True
        self.concurrency_group.start_new_thread(
            target=self._run_expiry_loop,
            name="update-apply-window",
            daemon=True,
            # The loop logs its own failures; a crash must not poison the root group.
            is_checked=False,
        )

    def stop(self) -> None:
        """Stop the expiry pass strand (it exits at its next wake)."""
        self._stop_event.set()

    def _window_seconds_for(self, apply_updated_at: datetime | None) -> float:
        """Restamp plus the template's grace, floored at one expiry interval so a stale apply
        still declines this instant's dispatch rather than opening an already-expired window."""
        if apply_updated_at is None:
            return self.fallback_window_seconds
        stale_at = apply_updated_at + timedelta(seconds=APPLY_GRACE_SECONDS)
        remaining = (stale_at - datetime.now(timezone.utc)).total_seconds()
        return max(remaining, _EXPIRY_PASS_INTERVAL_SECONDS)

    def open_window(self, agent_id: AgentId, *, apply_updated_at: datetime | None = None) -> None:
        """Arm the window for ``agent_id``: probe grace on, activity APPLYING.

        Re-arming extends the deadline rather than stacking. The row moves only while a
        run still owns it, so a verdict that landed during the guard's probe is kept.
        """
        window_seconds = self._window_seconds_for(apply_updated_at)
        deadline = time.monotonic() + window_seconds
        with self._lock:
            was_open = str(agent_id) in self._deadline_by_agent
            self._deadline_by_agent[str(agent_id)] = deadline
        self.tracker.begin_probe_grace(agent_id, ProbeGracePurpose.UPDATE_APPLY, deadline)
        self.store.set_activity(agent_id, UpdateActivity.APPLYING, only_from=IN_FLIGHT_ACTIVITIES)
        if was_open:
            logger.debug("Extended the update apply window for {} ({:.0f}s)", agent_id, window_seconds)
        else:
            logger.info("Opened the update apply window for {} ({:.0f}s)", agent_id, window_seconds)

    def close_window(self, agent_id: AgentId, *, reason: str) -> None:
        """Disarm the window for ``agent_id``. Idempotent."""
        aid_str = str(agent_id)
        with self._lock:
            was_open = self._deadline_by_agent.pop(aid_str, None) is not None
        self.tracker.end_probe_grace(agent_id, ProbeGracePurpose.UPDATE_APPLY)
        if was_open:
            logger.info("Closed the update apply window for {} ({})", agent_id, reason)

    def is_window_open(self, agent_id: AgentId) -> bool:
        """Whether an unexpired window is armed for ``agent_id``."""
        with self._lock:
            deadline = self._deadline_by_agent.get(str(agent_id))
        return deadline is not None and time.monotonic() < deadline

    def should_decline_recovery_dispatch(self, agent_id: AgentId) -> bool:
        """The dispatcher's race guard, answered on the stuck edge; no probe unless a run is in flight."""
        if self.is_window_open(agent_id):
            return True
        if not self.store.get(agent_id).is_run_in_flight:
            return False
        probe = self.probe_run(agent_id)
        if probe.is_probe_answered and not probe.is_apply_in_progress:
            logger.info(
                "Machine {} is stuck with an update in flight but no apply under way; treating it as a real outage",
                agent_id,
            )
            return False
        if probe.is_probe_answered:
            logger.info("Declined unattended recovery for {}: its update's apply is under way", agent_id)
        else:
            # Unreachable is not evidence of "no apply". Arming (not just declining) matters:
            # the stuck edge fires once per episode, so only the window's expiry can still
            # restart a machine that really did die.
            logger.info(
                "Declined unattended recovery for {}: its update is in flight and it could not say "
                "whether an apply is under way",
                agent_id,
            )
        self.open_window(agent_id, apply_updated_at=probe.apply_updated_at)
        return True

    def probe_run(self, agent_id: AgentId) -> UpdateRunProbe:
        """Ask the workspace whether an update is still happening in it."""
        chat_agent_name = self.store.get(agent_id).chat_agent_name
        result = self.mngr_caller.call(build_update_run_probe_args(agent_id), timeout=_RUN_PROBE_TIMEOUT_SECONDS)
        probe = parse_update_run_probe(result.stdout, chat_agent_name)
        if not probe.is_probe_answered:
            logger.debug(
                "Update run probe for {} produced no sentinel (exit {}): {}",
                agent_id,
                result.returncode,
                result.stderr.strip(),
            )
        return probe

    def run_expiry_pass(self) -> None:
        """Close every window whose deadline has passed, handing STUCK ones to recovery."""
        now = time.monotonic()
        with self._lock:
            # Popped under the same lock so a re-arm landing in between is not torn down.
            expired = [aid_str for aid_str, deadline in self._deadline_by_agent.items() if now >= deadline]
            for aid_str in expired:
                del self._deadline_by_agent[aid_str]
        for aid_str in expired:
            agent_id = AgentId(aid_str)
            if self.is_window_open(agent_id):
                continue
            self.tracker.end_probe_grace(agent_id, ProbeGracePurpose.UPDATE_APPLY)
            logger.info("Closed the update apply window for {} (deadline expired)", agent_id)
            self._hand_back_expired_window(agent_id)

    def _hand_back_expired_window(self, agent_id: AgentId) -> None:
        """Return one expired window's workspace to normal handling.

        A machine still STUCK gets its restart dispatched here: the stuck edge fires once
        per episode and was already declined, so nothing else would.
        """
        if self.store.get(agent_id).activity is UpdateActivity.APPLYING:
            self.store.set_activity(agent_id, UpdateActivity.RUNNING, only_from=frozenset({UpdateActivity.APPLYING}))
        if self.tracker.get_health(agent_id) is not AgentHealth.STUCK:
            return
        logger.warning(
            "The update apply window for {} expired with the machine still stuck; dispatching a restart", agent_id
        )
        self.dispatch_restart(agent_id)

    def _run_expiry_loop(self) -> None:
        # Sleeps on the group's shutdown event so a quit does not wait out an interval.
        while not self.concurrency_group.shutdown_event.wait(timeout=_EXPIRY_PASS_INTERVAL_SECONDS):
            if self._stop_event.is_set():
                break
            try:
                self.run_expiry_pass()
            except (MindError, MngrError, OSError, RuntimeError, ValueError) as e:
                # One machine's failure must not end the strand every windowed machine
                # depends on; anything outside this set is a bug and should kill it.
                logger.opt(exception=e).error("An update apply-window expiry pass failed; continuing")
        logger.debug("Exited the update apply-window expiry loop")
