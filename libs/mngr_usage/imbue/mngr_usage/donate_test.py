"""Unit tests for ``mngr_usage.donate`` -- the spare-capacity decision and argv builder.

The decision logic is a pure function over a usage snapshot, so it's tested here
directly with hand-built snapshots (no config/host setup). The ``donate`` command
wiring itself (gather -> decide -> ``mngr create``) is exercised end-to-end in the
integration tests / by ``mngr donate --dry-run``.
"""

from __future__ import annotations

import pytest

from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot
from imbue.mngr_usage.donate import CLAUDE_SOURCE
from imbue.mngr_usage.donate import FIVE_HOUR_WINDOW
from imbue.mngr_usage.donate import SEVEN_DAY_WINDOW
from imbue.mngr_usage.donate import build_create_argv
from imbue.mngr_usage.donate import evaluate_capacity
from imbue.mngr_usage.donate import weekly_pace_line

# A fixed "now" and a 7-day window; resets_at is set relative to NOW so a chosen
# fraction of the window has elapsed.
_NOW = 1_000_000
_WEEK_SECONDS = 7 * 24 * 60 * 60


def _seven_day(*, used_percentage: float, elapsed_fraction: float) -> WindowSnapshot:
    """A seven_day window whose derived elapsed% is ``elapsed_fraction * 100``."""
    seconds_until_reset = int(_WEEK_SECONDS * (1 - elapsed_fraction))
    return WindowSnapshot(
        used_percentage=used_percentage,
        window_seconds=_WEEK_SECONDS,
        resets_at=_NOW + seconds_until_reset,
    )


def _snapshot(**windows: WindowSnapshot) -> UsageSnapshot:
    return UsageSnapshot(source_name=CLAUDE_SOURCE, updated_at=_NOW, windows=windows)


def test_weekly_pace_line_starts_below_and_meets_the_plain_line() -> None:
    # Early in the cycle the ceiling sits ~30% under the plain used==elapsed line...
    assert weekly_pace_line(0.0) == pytest.approx(0.0)
    assert weekly_pace_line(50.0) == pytest.approx(42.5)  # 50 * (1 - 0.30 * 0.5)
    # ...and meets it exactly at the end of the cycle.
    assert weekly_pace_line(100.0) == pytest.approx(100.0)


def test_spare_when_five_hour_has_budget_and_weekly_under_pace() -> None:
    snap = _snapshot(
        **{
            FIVE_HOUR_WINDOW: WindowSnapshot(used_percentage=10.0),
            SEVEN_DAY_WINDOW: _seven_day(used_percentage=5.0, elapsed_fraction=0.5),
        }
    )
    decision = evaluate_capacity(snap, _NOW)
    assert decision.has_spare is True
    assert decision.five_hour_used_percentage == pytest.approx(10.0)
    assert decision.weekly_elapsed_percentage == pytest.approx(50.0)
    assert decision.weekly_pace_line == pytest.approx(42.5)


def test_no_spare_when_five_hour_window_is_near_exhausted() -> None:
    snap = _snapshot(
        **{
            FIVE_HOUR_WINDOW: WindowSnapshot(used_percentage=85.0),
            SEVEN_DAY_WINDOW: _seven_day(used_percentage=1.0, elapsed_fraction=0.5),
        }
    )
    # 85 >= 80 ceiling -> no spare, even though the week is wide open.
    assert evaluate_capacity(snap, _NOW).has_spare is False


def test_no_spare_when_weekly_usage_is_over_pace() -> None:
    snap = _snapshot(
        **{
            FIVE_HOUR_WINDOW: WindowSnapshot(used_percentage=10.0),
            # elapsed 50% -> pace line 42.5; 45 is over it.
            SEVEN_DAY_WINDOW: _seven_day(used_percentage=45.0, elapsed_fraction=0.5),
        }
    )
    assert evaluate_capacity(snap, _NOW).has_spare is False


def test_missing_snapshot_is_treated_as_fully_used() -> None:
    decision = evaluate_capacity(None, _NOW)
    assert decision.has_spare is False
    assert decision.five_hour_used_percentage == pytest.approx(100.0)
    assert decision.weekly_used_percentage == pytest.approx(100.0)


def test_snapshot_without_windows_is_conservative() -> None:
    decision = evaluate_capacity(_snapshot(), _NOW)
    assert decision.has_spare is False
    assert decision.five_hour_used_percentage == pytest.approx(100.0)
    assert decision.weekly_used_percentage == pytest.approx(100.0)


def test_window_without_derivable_elapsed_yields_zero_pace_and_no_spare() -> None:
    # A seven_day window with no window_seconds -> elapsed% not derivable -> 0 ->
    # pace line 0 -> weekly can never be "under pace", so never spare.
    snap = _snapshot(
        **{
            FIVE_HOUR_WINDOW: WindowSnapshot(used_percentage=1.0),
            SEVEN_DAY_WINDOW: WindowSnapshot(used_percentage=0.0, resets_at=_NOW + 1000),
        }
    )
    decision = evaluate_capacity(snap, _NOW)
    assert decision.weekly_elapsed_percentage == pytest.approx(0.0)
    assert decision.has_spare is False


def test_build_create_argv_matches_the_recipe_one_liner() -> None:
    assert build_create_argv("donate-extra-quota-bio", "document-review") == (
        "mngr",
        "create",
        "donate-extra-quota-bio",
        "claude",
        "--no-connect",
        "--message",
        "Use the document-review skill",
    )
