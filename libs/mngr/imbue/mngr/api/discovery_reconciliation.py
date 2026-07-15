from datetime import datetime
from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.pure import pure


@pure
def parse_event_timestamp(timestamp: IsoTimestamp) -> datetime:
    """Parse an event envelope's nanosecond ISO timestamp into a timezone-aware datetime.

    ``datetime.fromisoformat`` accepts the trailing ``Z`` and truncates fractional
    seconds beyond microseconds, which is sufficient for ordering events against a
    snapshot's discovery span.
    """
    return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))


@pure
def is_intervening_event(last_event_at: datetime | None, discovery_started_at: datetime) -> bool:
    """True if a state-change/destroy event for an item was seen during a snapshot's span.

    An item whose most recent incremental event landed at or after the snapshot's
    ``discovery_started_at`` reflects newer truth than that in-flight snapshot, so
    the snapshot must not overwrite it.
    """
    return last_event_at is not None and last_event_at >= discovery_started_at


class RemovedItemDecision(UpperCaseStrEnum):
    """Whether a snapshot-absent item should be retained or dropped from tracking."""

    RETAIN = auto()
    DROP = auto()


@pure
def classify_removed_item(is_provider_errored: bool, has_intervening_event: bool) -> RemovedItemDecision:
    """Decide whether an item absent from a fresh per-provider snapshot is gone or merely unknown.

    Retain (do not forget) when the provider errored this poll -- its absence
    reflects the failed read, not a confirmed removal -- or when a newer
    incremental event for the item landed during the snapshot's span (so the
    snapshot's omission is stale). Otherwise the item is confirmed gone: drop it.
    """
    if is_provider_errored or has_intervening_event:
        return RemovedItemDecision.RETAIN
    return RemovedItemDecision.DROP


@pure
def should_apply_snapshot_item(has_intervening_event: bool) -> bool:
    """True if a snapshot's value for an item should be applied (not clobbering newer truth).

    A snapshot must not overwrite an item whose own state-change/destroy event was
    observed at or after the snapshot's ``discovery_started_at`` -- that event is
    newer than this in-flight snapshot.
    """
    return not has_intervening_event
