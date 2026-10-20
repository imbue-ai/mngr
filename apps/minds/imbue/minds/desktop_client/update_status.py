"""The machine-readable status contract between a workspace's update files and the app.

The enums ``ui_models`` puts on the wire live here so the wire contract does not
import the detection module (and the discovery resolver behind it). The skill
writes ``data/.state/update-apply/run.json``, which the app polls over ``mngr
exec``: the run's start facts, three in-flight facts (worker, hold, apply), and
one terminal verdict. A new run's start overwrites it, so it always describes the
latest run. The app's in-workspace update program writes ``schedule.json`` and
``last-dispatch.json`` beside it and answers with an ``UpdateArmOutcome``; their
parsed forms live here too.
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
    NEEDS_DECISION = auto()
    """Nothing was applied: the update cannot keep something the user built. The worker's branch is kept,
    and the user's answer in the run's own chat is what lands it."""


# The verdicts the user has to be told went wrong: the run ended short, after its lead's best effort.
FAILURE_UPDATE_VERDICTS: Final[frozenset[UpdateVerdict]] = frozenset(
    {UpdateVerdict.STUCK, UpdateVerdict.REFUSED, UpdateVerdict.NEEDS_RECREATION}
)

# The verdicts after which the workspace serves the new build.
LANDED_UPDATE_VERDICTS: Final[frozenset[UpdateVerdict]] = frozenset(
    {UpdateVerdict.UPDATED, UpdateVerdict.UPDATED_WITH_REBUILD_ITEMS}
)


class UpdateAvailability(UpperCaseStrEnum):
    """The detection tri-state, plus the reverse-divergence case."""

    UP_TO_DATE = auto()
    """A positive read on both sides, and the workspace is at the ceiling."""
    OUT_OF_DATE = auto()
    """A positive read on both sides, and the workspace is on an earlier minor release than the ceiling."""
    PATCH_AVAILABLE = auto()
    """A positive read on both sides, and the workspace sorts below the ceiling within its minor release:
    never prompted for, but may be updated or scheduled by hand."""
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
    for the user, or the poll read it idle across consecutive polls. An agent
    whose chat is busy (a turn in flight or a background task pending) is not idle."""
    APPLYING = auto()
    """The merge-and-reveal is landing. The system-interface outage this causes is expected."""
    STALLED = auto()
    """A probe found no live update agent and no apply under way: the run is gone without a verdict."""


# Named once so the surfaces' read and the store's run-slot claim cannot disagree about what "in flight" means.
IN_FLIGHT_ACTIVITIES: Final[frozenset[UpdateActivity]] = frozenset(
    {UpdateActivity.STARTING, UpdateActivity.RUNNING, UpdateActivity.WAITING, UpdateActivity.APPLYING}
)


class UpdateSkipReason(UpperCaseStrEnum):
    """Why an update window closed without its scheduled run starting, as the workspace's dispatcher recorded it.

    Every one leaves the schedule armed for the next window.
    """

    UPDATE_IN_FLIGHT = auto()
    LEASE_HELD = auto()
    NEEDS_SIGN_IN = auto()
    DISPATCH_FAILED = auto()
    WINDOW_CLOSED = auto()


# Shown in the modal; each names the condition, not the code path. Window-relative
# rather than "last night": the window is configurable to any hours.
SKIP_REASON_MESSAGES: Final[dict[UpdateSkipReason, str]] = {
    UpdateSkipReason.UPDATE_IN_FLIGHT: "An update was already running in this workspace during the last update window.",
    UpdateSkipReason.LEASE_HELD: "Another update flow was working in this workspace during the last update window.",
    UpdateSkipReason.NEEDS_SIGN_IN: "No AI account was signed in to this workspace during the last update window.",
    UpdateSkipReason.DISPATCH_FAILED: "The update couldn't be started during the last update window.",
    UpdateSkipReason.WINDOW_CLOSED: "The last update window closed before the update could start.",
}


def format_update_window(window: tuple[int, int]) -> str:
    """Render an hour window the way a person reads a clock."""
    return f"{_format_hour(window[0])}-{_format_hour(window[1])}"


def _format_hour(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:00 {suffix}"


class UpdateStartedBy(UpperCaseStrEnum):
    """How an update run was started, as the in-workspace command stamped it."""

    SCHEDULED = auto()
    """The workspace's own cron tick dispatched it inside the update window."""
    IMMEDIATE = auto()
    """The user pressed "Update now" (or "Update all")."""


class UpdateArmOutcome(UpperCaseStrEnum):
    """What one run of the in-workspace update command came to, in its own words."""

    ARMED = auto()
    """The schedule file is written; the workspace's cron tick starts the run inside the window."""
    CREATED = auto()
    """The update chat was created and is starting."""
    ALREADY_RUNNING = auto()
    """An update is already in flight there; the existing run is the answer."""
    NEEDS_SIGN_IN = auto()
    """The workspace has no signed-in AI account its chats could run on."""
    CHAT_APP_UNREACHABLE = auto()
    """The workspace's chat app did not answer, and the bare create behind it failed too."""
    CREATE_FAILED = auto()
    """The workspace's chat app answered, but did not create the chat; its words are the detail."""
    NO_CRON = auto()
    """The workspace has no cron program, so nothing inside it could start a scheduled run."""
    TOO_OLD_TO_UPDATE = auto()
    """The workspace predates the update-self skill; there is nothing to run."""


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


class UpdateDispatchFailure(FrozenModel):
    """Why an asked-for run never went out: the app's sentence, and what the machine, mngr, or the host start said."""

    message: str = Field(description="One plain-English sentence saying what did not happen")
    detail: str = Field(
        default="",
        description="Bounded verbatim output from the machine, mngr, or the host start; '' when there was none",
    )


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
    started_by: UpdateStartedBy | None = Field(
        default=None, description="How the run was started; None when the record does not say (an older template)"
    )

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
    return parse_update_run_record(raw)


def _parse_started_by(value: object) -> UpdateStartedBy | None:
    if value is None:
        return None
    try:
        return UpdateStartedBy(str(value).upper())
    except ValueError:
        logger.debug("Reading an update run record's unknown start kind {!r} as unknown", value)
        return None


def parse_update_run_record(raw: object) -> UpdateRunStatus | None:
    """Parse one already-decoded ``run.json`` object, or None when it is not one; lenient as above."""
    if not isinstance(raw, dict):
        logger.warning("Dropped an update run record that is not an object: {!r}", str(raw)[:200])
        return None
    record: dict[str, object] = {str(key): value for key, value in raw.items()}
    verdict: UpdateVerdict | None = None
    raw_verdict = record.get("verdict")
    if raw_verdict is not None:
        try:
            verdict = UpdateVerdict(str(raw_verdict))
        except ValueError:
            logger.warning("Reading an update run record with an unknown verdict {!r} as still running", raw_verdict)
    return UpdateRunStatus(
        chat_agent_name=str(record.get("chat_agent_name") or ""),
        started_at=_parse_epoch(record.get("started_at")),
        worker_agent_name=str(record.get("worker_agent_name") or ""),
        is_holding=bool(record.get("is_holding", False)),
        hold_detail=str(record.get("hold_detail") or ""),
        apply_phase=str(record.get("apply_phase") or ""),
        apply_updated_at=_parse_epoch(record.get("apply_updated_at")),
        verdict=verdict,
        detail=str(record.get("detail") or ""),
        resulting_ref=str(record.get("resulting_ref") or ""),
        in_place_compatible_ref=str(record.get("in_place_compatible_ref") or ""),
        verdict_at=_parse_epoch(record.get("verdict_at")),
        started_by=_parse_started_by(record.get("started_by")),
    )


class UpdateScheduleRecord(FrozenModel):
    """One workspace's armed schedule, parsed from the file its in-workspace update command writes there.

    The app writes the ceiling, the window, the zone and the override; the workspace's
    dispatcher writes the chat it started this window and why the last window was skipped.
    """

    armed_at: datetime | None = Field(description="When the schedule was armed (UTC), None if unreadable")
    ceiling_ref: str = Field(description="The app's release ceiling the run targets unless an override names another")
    window_start_hour: int = Field(description="Local hour the window opens at, in the file's zone")
    window_end_hour: int = Field(description="Local hour the window closes at, in the file's zone")
    zone: str = Field(description="The IANA zone the window's hours are read in; '' for the workspace's own clock")
    override_ref: str = Field(description="The exact version the user typed, '' for the ceiling")
    marked_chat_name: str = Field(description="The update chat the dispatcher started this window; '' for none")
    pending_skip_reason: str = Field(description="Why the open window's run has not started yet; '' for none")
    last_skip_reason: str = Field(description="Why the last window closed without a start; '' for none")
    last_skip_at: datetime | None = Field(description="When that window closed (UTC), None for none")

    @property
    def target_ref(self) -> str:
        """The ref the scheduled run targets: the override when the user typed one, else the ceiling."""
        return self.override_ref or self.ceiling_ref


def _parse_hour(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 23:
        return None
    return value


def parse_update_schedule_record(raw: object) -> UpdateScheduleRecord | None:
    """Parse one already-decoded schedule file, or None when it is not a usable one.

    Lenient per field, as a file a newer app wrote must stay readable; only a window with no
    readable hours is dropped, since nothing could say when it runs.
    """
    if not isinstance(raw, dict):
        logger.warning("Dropped an update schedule file that is not an object: {!r}", str(raw)[:200])
        return None
    record: dict[str, object] = {str(key): value for key, value in raw.items()}
    start_hour = _parse_hour(record.get("window_start_hour"))
    end_hour = _parse_hour(record.get("window_end_hour"))
    if start_hour is None or end_hour is None:
        logger.warning("Dropped an update schedule file with no readable window: {!r}", str(raw)[:200])
        return None
    return UpdateScheduleRecord(
        armed_at=_parse_epoch(record.get("armed_at")),
        ceiling_ref=str(record.get("ceiling") or ""),
        window_start_hour=start_hour,
        window_end_hour=end_hour,
        zone=str(record.get("zone") or ""),
        override_ref=str(record.get("override_ref") or ""),
        marked_chat_name=str(record.get("marked_chat") or ""),
        pending_skip_reason=str(record.get("pending_skip_reason") or ""),
        last_skip_reason=str(record.get("last_skip_reason") or ""),
        last_skip_at=_parse_epoch(record.get("last_skip_at")),
    )


class UpdateLastDispatch(FrozenModel):
    """The update chat the workspace's update program last created, as it recorded it."""

    chat_agent_name: str = Field(description="The chat's agent name")
    chat_id: str = Field(description="The chat's id, for opening it; '' when the create did not say")
    started_by: UpdateStartedBy | None = Field(description="Whether a schedule or a press started it")
    ceiling_ref: str = Field(description="The app ceiling it was pinned to; '' when the record does not say")


def parse_update_last_dispatch(raw: object) -> UpdateLastDispatch | None:
    """Parse one already-decoded ``last-dispatch.json``, or None when it is not one."""
    if not isinstance(raw, dict):
        logger.warning("Dropped an update dispatch record that is not an object: {!r}", str(raw)[:200])
        return None
    record: dict[str, object] = {str(key): value for key, value in raw.items()}
    chat_agent_name = str(record.get("chat_agent_name") or "")
    if not chat_agent_name:
        return None
    return UpdateLastDispatch(
        chat_agent_name=chat_agent_name,
        chat_id=str(record.get("chat_id") or ""),
        started_by=_parse_started_by(record.get("started_by")),
        ceiling_ref=str(record.get("ceiling") or ""),
    )
