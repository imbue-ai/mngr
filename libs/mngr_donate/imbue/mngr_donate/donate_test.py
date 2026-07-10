"""Unit tests for ``mngr_usage.donate`` -- the spare-capacity decision and argv builder.

The decision logic is a pure function over a usage snapshot, so it's tested here
directly with hand-built snapshots (no config/host setup). The ``donate`` command
wiring itself (gather -> decide -> ``mngr create``) is exercised end-to-end in the
integration tests / by ``mngr donate --dry-run``.
"""

from __future__ import annotations

import pytest

from imbue.mngr_donate.donate import CLAUDE_SOURCE
from imbue.mngr_donate.donate import FIVE_HOUR_WINDOW
from imbue.mngr_donate.donate import SEVEN_DAY_WINDOW
from imbue.mngr_donate.donate import build_create_argv
from imbue.mngr_donate.donate import build_destroy_argv
from imbue.mngr_donate.donate import build_donation_message
from imbue.mngr_donate.donate import build_launchd_plist
from imbue.mngr_donate.donate import evaluate_capacity
from imbue.mngr_donate.donate import weekly_pace_line
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot

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
    # 50 * (1 - 0.30 * 0.5) == 42.5
    assert weekly_pace_line(50.0) == pytest.approx(42.5)
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
    assert decision.has_usage_data is True
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


def test_missing_snapshot_is_treated_as_fully_used_but_flagged_as_no_data() -> None:
    decision = evaluate_capacity(None, _NOW)
    assert decision.has_spare is False
    # Conservative percentages, but flagged so the caller says "can't tell", not "maxed out".
    assert decision.has_usage_data is False
    assert decision.five_hour_used_percentage == pytest.approx(100.0)
    assert decision.weekly_used_percentage == pytest.approx(100.0)


def test_partial_reading_counts_as_having_usage_data() -> None:
    # Only the 5h window has a reading; that's still real data, not a blank tick.
    snap = _snapshot(**{FIVE_HOUR_WINDOW: WindowSnapshot(used_percentage=90.0)})
    decision = evaluate_capacity(snap, _NOW)
    assert decision.has_usage_data is True
    # 90 >= 80 ceiling
    assert decision.has_spare is False


def test_snapshot_without_windows_is_conservative() -> None:
    decision = evaluate_capacity(_snapshot(), _NOW)
    assert decision.has_spare is False
    # A snapshot with no windows carries no readings -> treated as "no data".
    assert decision.has_usage_data is False
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


def test_build_create_argv_launches_a_headless_agent_that_skips_permissions() -> None:
    argv = build_create_argv("donate-extra-quota-bio", "/host/donate-skills/document-review")
    assert argv[:10] == (
        "mngr",
        "create",
        "donate-extra-quota-bio",
        "headless_claude",
        "--foreground",
        "--no-ensure-clean",
        # Force shared config so claude uses/refreshes the real keychain token.
        "-S",
        "agent_types.headless_claude.isolate_local_config_dir=false",
        "--message",
        build_donation_message("/host/donate-skills/document-review"),
    )
    assert argv[10:] == (
        "--",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--dangerously-skip-permissions",
    )


def test_build_donation_message_points_the_agent_at_the_skill_dir() -> None:
    message = build_donation_message("/host/donate-skills/document-review")
    # Points at the assembled cache dir (not Claude skill auto-discovery) and its SKILL.md.
    assert "/host/donate-skills/document-review/SKILL.md" in message
    assert "client.py" in message


def test_build_launchd_plist_embeds_program_env_and_interval() -> None:
    plist = build_launchd_plist(
        "/venv/bin/mngr", "/repo", "document-review", "donate-extra-quota-bio", "/logs/schedule.log", "/usr/bin:/bin", 600
    )
    # Runs mngr donate directly (no shell), in the repo, with the given PATH + interval.
    assert "<string>/venv/bin/mngr</string>" in plist
    assert "<string>donate</string>" in plist
    # WorkingDirectory
    assert "<string>/repo</string>" in plist
    # EnvironmentVariables PATH
    assert "<string>/usr/bin:/bin</string>" in plist
    # StartInterval seconds (600s == 10 min)
    assert "<integer>600</integer>" in plist
    assert "<string>/logs/schedule.log</string>" in plist
    # Defaults are omitted from ProgramArguments (kept minimal).
    assert "--skill" not in plist
    assert "--agent-name" not in plist


def test_build_launchd_plist_includes_non_default_options() -> None:
    plist = build_launchd_plist(
        "/venv/bin/mngr", "/repo", "other-skill", "my-agent", "/logs/schedule.log", "/usr/bin", 60
    )
    assert "<string>--skill</string>" in plist
    assert "<string>other-skill</string>" in plist
    assert "<string>--agent-name</string>" in plist
    assert "<string>my-agent</string>" in plist


def test_build_destroy_argv_force_removes_a_stale_agent_by_name() -> None:
    assert build_destroy_argv("donate-extra-quota-bio") == (
        "mngr",
        "destroy",
        "donate-extra-quota-bio",
        "--force",
    )
