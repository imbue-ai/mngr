"""The machine-readable status contract between ``/update-self`` and the app.

The enums ``ui_models`` puts on the wire live here so the wire contract does not
import the detection module (and the discovery resolver behind it). The skill
writes one file, ``data/.state/update-apply/run.json``, which the app polls over
``mngr exec``: the run's start facts, three in-flight facts (worker, hold, apply),
and one terminal verdict. A new run's start overwrites it, so it always describes
the latest run.
"""

import json
from datetime import datetime
from datetime import timezone
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class UpdateVerdict(UpperCaseStrEnum):
    """How an update run ended. Exactly one is recorded per run."""

    UPDATED = auto()
    """The workspace is now on the target ref and healthy."""
    UPDATED_WITH_REBUILD_ITEMS = auto()
    """Applied, but something the agent could not finish needs a person."""
    ALREADY_CURRENT = auto()
    """Nothing to apply. Not a failure, unlike REFUSED: a no-op is an ordinary outcome
    for a workspace read from a stale label or with no readable version."""
    NEEDS_RECREATION = auto()
    """The target cannot be applied in place. Surfaced like any other failure: the run's own chat says why."""
    STUCK = auto()
    """The run could not finish and could not clean up after itself."""
    REFUSED = auto()
    """Nothing was applied -- e.g. no target was admissible, or the apply rolled back."""


class UpdateAvailability(UpperCaseStrEnum):
    """The detection tri-state, plus the reverse-divergence case."""

    UP_TO_DATE = auto()
    """A positive read on both sides, and the workspace is at the ceiling."""
    OUT_OF_DATE = auto()
    """A positive read on both sides, and the workspace sorts below the ceiling."""
    UNKNOWN = auto()
    """No comparable version on one side or the other; no update UI, no dispatch."""
    APP_BEHIND = auto()
    """The workspace is NEWER than this app's ceiling -- there is nothing to run here."""
    NEEDS_RECREATION = auto()
    """A positive read that the workspace predates the oldest release that can be updated in place:
    the user must create a new workspace and migrate into it."""


class UpdateUnknownReason(UpperCaseStrEnum):
    """Which side of the comparison had no version, when the answer is UNKNOWN.

    A branch-pinned build reads UNKNOWN for every machine, and must not be told as the machine being unreadable.
    """

    NO_APP_VERSION = auto()
    """This build names no supported release, so nothing can be compared against it."""
    NO_MACHINE_VERSION = auto()
    """No ``minds-v*`` version could be read for the machine itself."""


class UpdateActivity(UpperCaseStrEnum):
    """What an update run for this workspace is currently doing, as the app can tell."""

    IDLE = auto()
    """No run in flight."""
    STARTING = auto()
    """The app is still spawning the run's chat; it does not exist in the workspace yet."""
    RUNNING = auto()
    """The run's chat exists and is preparing in its own branch and worktree; the live workspace is untouched."""
    WAITING = auto()
    """The run's chat agent is alive but idle: its record says it is holding
    for the user, or the poll read it idle across consecutive polls."""
    APPLYING = auto()
    """The merge-and-reveal is landing. The system-interface outage this causes is expected."""
    STALLED = auto()
    """A probe found no live update agent and no apply under way: the run is gone without a verdict."""


# Named once so the surfaces' read and the store's run-slot claim cannot disagree about what "in flight" means.
IN_FLIGHT_ACTIVITIES: Final[frozenset[UpdateActivity]] = frozenset(
    {UpdateActivity.STARTING, UpdateActivity.RUNNING, UpdateActivity.WAITING, UpdateActivity.APPLYING}
)


class UpdateSkipReason(UpperCaseStrEnum):
    """Why a scheduled attempt did not run; all re-arm except ``ALREADY_UP_TO_DATE``."""

    WORKSPACE_UNREACHABLE = auto()
    CHATS_RUNNING = auto()
    UPDATE_IN_FLIGHT = auto()
    ALREADY_UP_TO_DATE = auto()
    DISPATCH_FAILED = auto()


# Shown in the modal; each names the condition, not the code path. Window-relative
# rather than "last night": the window is configurable to any hours.
SKIP_REASON_MESSAGES: Final[dict[UpdateSkipReason, str]] = {
    UpdateSkipReason.WORKSPACE_UNREACHABLE: "This machine couldn't be reached during the last update window.",
    UpdateSkipReason.CHATS_RUNNING: "Agents were still working in this machine during the last update window.",
    UpdateSkipReason.UPDATE_IN_FLIGHT: "An update was already running in this machine.",
    UpdateSkipReason.ALREADY_UP_TO_DATE: "This machine was already up to date.",
    UpdateSkipReason.DISPATCH_FAILED: "The update couldn't be started during the last update window.",
}


def describe_skip_reason(recorded_reason: str) -> str:
    """The modal's line for a recorded skip; '' for none, or for one this build does not know (a newer build's)."""
    if not recorded_reason:
        return ""
    try:
        reason = UpdateSkipReason(recorded_reason)
    except ValueError:
        # Debug, not warning: this runs on every composed read of the row, not once per record.
        logger.debug("Dropping an update-schedule skip reason this build does not know: {!r}", recorded_reason)
        return ""
    return SKIP_REASON_MESSAGES[reason]


class UpdateRunStatus(FrozenModel):
    """One parsed ``run.json``: the latest run's start facts, its in-flight facts, and its verdict once it has one."""

    chat_agent_name: str = Field(default="", description="Name of the run's chat agent inside the workspace")
    started_at: datetime | None = Field(default=None, description="When the run began (UTC), None if unreadable")
    worker_agent_name: str = Field(
        default="",
        description="Name of the background worker the run's chat has handed its work to; '' when none is recorded",
    )
    is_holding: bool = Field(
        default=False, description="Whether the run has stopped to wait for the user; False while it is moving"
    )
    hold_detail: str = Field(default="", description="One line naming what the run is waiting on, for the modal")
    apply_phase: str = Field(
        default="",
        description="The apply's last completed phase while it is landing (mirrored from the apply's own marker "
        "on every restamp); '' when no apply is under way",
    )
    apply_updated_at: datetime | None = Field(
        default=None, description="When the apply last moved (UTC); None when no apply is under way or unreadable"
    )
    verdict: UpdateVerdict | None = Field(default=None, description="The terminal verdict, None while the run goes")
    detail: str = Field(default="", description="One-line human-readable summary, for the modal")
    resulting_ref: str = Field(default="", description="Ref the workspace is on now (success verdicts)")
    in_place_compatible_ref: str = Field(
        default="",
        description="On REFUSED/NEEDS_RECREATION, the newest ref that could still be applied in place, if any",
    )
    verdict_at: datetime | None = Field(default=None, description="When the verdict landed (UTC), None until it has")

    @property
    def is_apply_in_progress(self) -> bool:
        """Whether the record says the apply is landing right now."""
        return bool(self.apply_phase)


def _parse_epoch(value: object) -> datetime | None:
    """An epoch-seconds field as an aware UTC datetime, or None if it is not one."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_update_run_status(text: str) -> UpdateRunStatus | None:
    """Parse one ``run.json``'s content, or None when it is not a readable record.

    Lenient per field: an unknown verdict (a newer skill) reads as "no verdict
    yet", so the liveness read still governs the row.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Dropped an unparseable update run record: {}", e)
        return None
    if not isinstance(raw, dict):
        logger.warning("Dropped an update run record that is not an object: {!r}", text[:200])
        return None
    verdict: UpdateVerdict | None = None
    raw_verdict = raw.get("verdict")
    if raw_verdict is not None:
        try:
            verdict = UpdateVerdict(str(raw_verdict))
        except ValueError:
            logger.warning("Reading an update run record with an unknown verdict {!r} as still running", raw_verdict)
    return UpdateRunStatus(
        chat_agent_name=str(raw.get("chat_agent_name") or ""),
        started_at=_parse_epoch(raw.get("started_at")),
        worker_agent_name=str(raw.get("worker_agent_name") or ""),
        is_holding=bool(raw.get("is_holding", False)),
        hold_detail=str(raw.get("hold_detail") or ""),
        apply_phase=str(raw.get("apply_phase") or ""),
        apply_updated_at=_parse_epoch(raw.get("apply_updated_at")),
        verdict=verdict,
        detail=str(raw.get("detail") or ""),
        resulting_ref=str(raw.get("resulting_ref") or ""),
        in_place_compatible_ref=str(raw.get("in_place_compatible_ref") or ""),
        verdict_at=_parse_epoch(raw.get("verdict_at")),
    )
