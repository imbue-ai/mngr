"""Restating the shipped progress seeds from recorded histories.

A device learns its own durations after its first run, so the shipped seeds in
``create_progress_defaults.toml`` only decide the first bar a fresh install
draws. They still drift as the template and the providers change, so a
scheduled job measures a create, feeds its history through this module, and
opens a pull request when a seed has moved far enough to matter.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.create_progress_history import ShippedProgressDefaults
from imbue.minds.desktop_client.create_progress_history import format_shipped_defaults
from imbue.minds.desktop_client.create_progress_history import history_events_from_lines
from imbue.minds.desktop_client.create_progress_history import measurements_by_scope_from_events
from imbue.minds.desktop_client.create_progress_history import median_of_recent_measurements
from imbue.minds.desktop_client.create_progress_history import parse_shipped_defaults

# A seed moves only when the measured median differs from it by at least this
# fraction, so one slightly faster run does not churn the file.
DEFAULT_DRIFT_THRESHOLD: Final[float] = 0.3
# No seed goes below this: a step measured at nothing (a skipped probe, a cached
# clone) must still own a sliver of the bar and must stay a positive weight.
MIN_SEED_SECONDS: Final[float] = 1.0


class SeedChange(FrozenModel):
    """One seed the measurements would restate."""

    scope: str = Field(description="The scope the step ran in, such as 'create:DOCKER'")
    step_id: str = Field(description="The step whose seed moves")
    previous_seconds: float | None = Field(description="The seed as shipped; None for a step the file did not seed")
    measured_seconds: float = Field(description="The median of the recent measurements")


class SeedRemoval(FrozenModel):
    """One seed the measurements show no longer exists."""

    scope: str = Field(description="The scope the seed is in")
    step_id: str = Field(description="The seed that goes")
    previous_seconds: float = Field(description="The seed as shipped")


class SeedSyncPlan(FrozenModel):
    """What a sync would change, and the seeds it would leave alone."""

    changes: tuple[SeedChange, ...] = Field(description="The seeds that move, in scope then step order")
    removals: tuple[SeedRemoval, ...] = Field(default=(), description="The seeds that go, in scope then step order")
    unchanged_count: int = Field(description="Measured seeds that stay within the drift threshold")


class HistoryMeasurements(FrozenModel):
    """What one or more history files say: the durations per step, and every step they mention at all."""

    by_scope: Mapping[str, Mapping[str, Sequence[float]]] = Field(description="Measurements per scope and step id")
    seen_by_scope: Mapping[str, AbstractSet[str]] = Field(
        description="Every step id recorded per scope, measured or skipped: the steps the operation still has"
    )

    def merged_with(self, other: "HistoryMeasurements") -> "HistoryMeasurements":
        by_scope: dict[str, dict[str, list[float]]] = {
            scope: {step_id: list(samples) for step_id, samples in by_step.items()}
            for scope, by_step in self.by_scope.items()
        }
        for scope, by_step in other.by_scope.items():
            for step_id, samples in by_step.items():
                by_scope.setdefault(scope, {}).setdefault(step_id, []).extend(samples)
        seen_by_scope = {scope: set(seen) for scope, seen in self.seen_by_scope.items()}
        for scope, seen in other.seen_by_scope.items():
            seen_by_scope.setdefault(scope, set()).update(seen)
        return HistoryMeasurements(by_scope=by_scope, seen_by_scope=seen_by_scope)


@pure
def measurements_from_history_text(history_text: str) -> HistoryMeasurements:
    """What a history file's text measured, oldest first; unreadable lines are skipped."""
    events = history_events_from_lines(history_text.splitlines())
    seen_by_scope: dict[str, set[str]] = {}
    for event in events:
        seen_by_scope.setdefault(event.scope, set()).add(event.step_id)
    return HistoryMeasurements(by_scope=measurements_by_scope_from_events(events), seen_by_scope=seen_by_scope)


@pure
def _measured_ancestor_id(step_id: str, measured_step_ids: AbstractSet[str]) -> str | None:
    """The nearest step above ``step_id`` by its id that was measured, or None when no prefix of the id was.

    ``docker.build`` for ``docker.build.export``, and ``setting_up_host`` for
    ``setting_up_host.inner.command_0`` (whose ``inner`` segment is a prefix, not
    a step); a backend-namespaced id (``vps.wait_ssh``) has no measured prefix.
    """
    ancestor, dot, _ = step_id.rpartition(".")
    while dot:
        if ancestor in measured_step_ids:
            return ancestor
        ancestor, dot, _ = ancestor.rpartition(".")
    return None


@pure
def plan_seed_sync(
    defaults: ShippedProgressDefaults, measurements: HistoryMeasurements, drift_threshold: float
) -> SeedSyncPlan:
    """The seeds whose measured median has drifted past ``drift_threshold`` of the shipped value, or is new.

    A shipped seed nested by its id under a step the measurements cover (a
    build stage under ``docker.build``, a post-host-create command under
    ``setting_up_host``) that the history never mentions, measured or skipped,
    is a step the operation no longer has, and goes; a measured step whose
    history mentions nothing under it at all was reported without its stages,
    which says nothing about them, so its seeds stay.
    """
    changes: list[SeedChange] = []
    removals: list[SeedRemoval] = []
    unchanged_count = 0
    measurements_by_scope = measurements.by_scope
    for scope in sorted(measurements_by_scope):
        shipped = defaults.seconds_by_step_id_by_scope.get(scope, {})
        for step_id in sorted(measurements_by_scope[scope]):
            samples = measurements_by_scope[scope][step_id]
            if not samples:
                continue
            measured = max(MIN_SEED_SECONDS, round(median_of_recent_measurements(samples), 1))
            previous = shipped.get(step_id)
            if previous is not None and abs(measured - previous) <= drift_threshold * previous:
                unchanged_count += 1
                continue
            changes.append(
                SeedChange(scope=scope, step_id=step_id, previous_seconds=previous, measured_seconds=measured)
            )
        seen = measurements.seen_by_scope.get(scope, set())
        measured_step_ids = measurements_by_scope[scope].keys()
        ancestors_with_children = {
            ancestor
            for ancestor in (_measured_ancestor_id(step_id, measured_step_ids) for step_id in seen)
            if ancestor is not None
        }
        for step_id in sorted(shipped):
            if step_id in seen:
                continue
            ancestor = _measured_ancestor_id(step_id, measured_step_ids)
            if ancestor is None or ancestor not in ancestors_with_children:
                continue
            removals.append(SeedRemoval(scope=scope, step_id=step_id, previous_seconds=shipped[step_id]))
    return SeedSyncPlan(changes=tuple(changes), removals=tuple(removals), unchanged_count=unchanged_count)


@pure
def apply_seed_sync(defaults: ShippedProgressDefaults, plan: SeedSyncPlan) -> ShippedProgressDefaults:
    by_scope = {scope: dict(seconds) for scope, seconds in defaults.seconds_by_step_id_by_scope.items()}
    for change in plan.changes:
        by_scope.setdefault(change.scope, {})[change.step_id] = change.measured_seconds
    for removal in plan.removals:
        by_scope[removal.scope].pop(removal.step_id, None)
    return ShippedProgressDefaults(
        default_step_seconds=defaults.default_step_seconds, seconds_by_step_id_by_scope=by_scope
    )


@pure
def format_seed_sync_report(plan: SeedSyncPlan) -> str:
    """A markdown account of the plan, for the job summary and the pull request body."""
    if not plan.changes and not plan.removals:
        return f"No progress seed moved; {plan.unchanged_count} measured seed(s) stayed within the drift threshold.\n"
    lines = [
        "Restated the create progress seeds from the latest measurements.",
        "",
        "| scope | step | shipped (s) | measured (s) |",
        "| --- | --- | ---: | ---: |",
    ]
    for change in plan.changes:
        previous = "new" if change.previous_seconds is None else f"{change.previous_seconds:g}"
        lines.append(f"| `{change.scope}` | `{change.step_id}` | {previous} | {change.measured_seconds:g} |")
    for removal in plan.removals:
        lines.append(f"| `{removal.scope}` | `{removal.step_id}` | {removal.previous_seconds:g} | gone |")
    lines.extend(("", f"{plan.unchanged_count} measured seed(s) stayed within the drift threshold."))
    return "\n".join(lines) + "\n"


def sync_seed_file(
    defaults_path: Path, history_paths: Sequence[Path], drift_threshold: float, *, is_check_only: bool
) -> SeedSyncPlan:
    """Plan the sync from every history file and, unless checking only, rewrite the seed file."""
    defaults = parse_shipped_defaults(defaults_path.read_text())
    measurements = HistoryMeasurements(by_scope={}, seen_by_scope={})
    for history_path in history_paths:
        measurements = measurements.merged_with(measurements_from_history_text(history_path.read_text()))
    plan = plan_seed_sync(defaults, measurements, drift_threshold)
    if (plan.changes or plan.removals) and not is_check_only:
        defaults_path.write_text(format_shipped_defaults(apply_seed_sync(defaults, plan)))
    return plan
