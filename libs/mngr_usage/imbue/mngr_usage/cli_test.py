"""Unit tests for mngr_usage.cli (agent-agnostic CLI + walk-by-convention discovery)."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.config.consts import ROOT_CONFIG_FILENAME
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr_usage.api import collapse_by_source
from imbue.mngr_usage.api import last_valid_event_from_content
from imbue.mngr_usage.api import snapshot_from_event
from imbue.mngr_usage.cli import _build_render_model
from imbue.mngr_usage.cli import _flatten_primary_for_template
from imbue.mngr_usage.cli import _format_duration
from imbue.mngr_usage.cli import _format_human_line
from imbue.mngr_usage.cli import _format_reset_phrase
from imbue.mngr_usage.cli import _parse_max_age
from imbue.mngr_usage.cli import usage
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot


def _write_event(events_file: Path, event: dict[str, Any]) -> None:
    """Append a JSONL event line to ``events_file``, creating parents as needed."""
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _make_event(
    timestamp: str,
    used_percentage: float | None = 11.0,
    resets_at: int | None = 1778280000,
) -> dict:
    """Construct an event matching the writer's emitted shape."""
    return {
        "source": "claude/rate_limits",
        "type": "rate_limit_snapshot",
        "event_id": "evt-test123",
        "timestamp": timestamp,
        "rate_limits": {
            "five_hour": {"used_percentage": used_percentage, "resets_at": resets_at},
        },
    }


# =============================================================================
# Pure helpers
# =============================================================================


def test_parse_max_age_accepts_units() -> None:
    assert _parse_max_age("300") == 300
    assert _parse_max_age("60s") == 60
    assert _parse_max_age("5m") == 300
    assert _parse_max_age("2h") == 7200
    assert _parse_max_age("1d") == 86400
    assert _parse_max_age(None) is None
    assert _parse_max_age("") is None


def test_parse_max_age_rejects_bad_input() -> None:
    with pytest.raises(UserInputError):
        _parse_max_age("forever")


def test_format_duration_hits_each_branch() -> None:
    assert _format_duration(0) == "now"
    assert _format_duration(-1) == "now"
    assert _format_duration(45) == "45s"
    assert _format_duration(60) == "1m"
    assert _format_duration(125) == "2m 5s"
    assert _format_duration(3600) == "1h"
    assert _format_duration(7325) == "2h 2m"
    assert _format_duration(86400) == "1d"
    assert _format_duration(360000) == "4d 4h"


def test_format_reset_phrase_handles_past_present_future() -> None:
    assert _format_reset_phrase(resets_at=1500, now=1000) == "resets in 8m 20s"
    assert _format_reset_phrase(resets_at=1000, now=1000) == "just reset"
    assert _format_reset_phrase(resets_at=970, now=1000) == "reset 30s ago"
    assert _format_reset_phrase(resets_at=400, now=1000) == "reset 10m ago"


def test_format_human_line_uses_past_tense_after_reset() -> None:
    snap = WindowSnapshot(used_percentage=11.0, resets_at=970)
    assert _format_human_line("5h", snap, now=1000) == "5h: 11% used, reset 30s ago"


def test_format_human_line_no_data_drops_reset_suffix() -> None:
    snap = WindowSnapshot(used_percentage=None, resets_at=1000)
    assert _format_human_line("5h", snap, now=1000) == "5h: no data"


# =============================================================================
# Event reading + snapshot building
# =============================================================================


def test_last_valid_event_picks_last_valid_line() -> None:
    """A truncated/garbage trailing line is skipped; the previous valid line wins."""
    content = (
        json.dumps(_make_event("2026-05-08T10:00:00.000000000Z"))
        + "\n"
        + json.dumps(_make_event("2026-05-08T11:00:00.000000000Z"))
        + "\n"
        + "{not valid json"
    )
    event = last_valid_event_from_content(content, "test")
    assert event is not None
    assert event["timestamp"] == "2026-05-08T11:00:00.000000000Z"


def test_last_valid_event_returns_none_when_no_valid_lines() -> None:
    assert last_valid_event_from_content("garbage\nstill garbage\n", "test") is None


def test_last_valid_event_returns_none_for_empty_content() -> None:
    assert last_valid_event_from_content("", "test") is None
    assert last_valid_event_from_content("\n\n", "test") is None


def testsnapshot_from_event_drops_events_without_rate_limits() -> None:
    """An event line without a rate_limits field can't make a useful snapshot."""
    event = {
        "source": "claude/rate_limits",
        "timestamp": "2026-05-08T10:00:00.000000000Z",
        "type": "rate_limit_snapshot",
        # no rate_limits field
    }
    assert snapshot_from_event(event, source_name="claude") is None


def testsnapshot_from_event_drops_unparseable_timestamps() -> None:
    event = _make_event("not-a-timestamp")
    assert snapshot_from_event(event, source_name="claude") is None


def testsnapshot_from_event_round_trips_window_data() -> None:
    event = _make_event("2026-05-08T10:00:00.000000000Z", used_percentage=42.5, resets_at=1778280000)
    snap = snapshot_from_event(event, source_name="claude")
    assert snap is not None
    assert snap.source_name == "claude"
    assert snap.windows["five_hour"].used_percentage == 42.5
    assert snap.windows["five_hour"].resets_at == 1778280000


# NOTE: the old filesystem-walking _gather_snapshots(host_dir) tests have been
# removed. The walker now uses list_agents + the events API; per-agent reads
# are exercised end-to-end via the test_usage_command_* tests below, which
# plant events files into a real local agent's state dir.


# =============================================================================
# Snapshot picking + render model
# =============================================================================


def _snap(name: str = "x", at: int = 1000, percentage: float | None = 50.0) -> UsageSnapshot:
    return UsageSnapshot(
        source_name=name,
        updated_at=at,
        windows={"five_hour": WindowSnapshot(used_percentage=percentage, resets_at=at + 3600)},
    )


def testcollapse_by_source_picks_freshest_per_source() -> None:
    """Multiple agents writing to the same source should collapse to the freshest."""
    older_claude = _snap(name="claude", at=1000, percentage=10.0)
    newer_claude = _snap(name="claude", at=2000, percentage=20.0)
    only_opencode = _snap(name="opencode", at=1500, percentage=30.0)
    result = collapse_by_source([older_claude, newer_claude, only_opencode])
    assert {s.source_name for s in result} == {"claude", "opencode"}
    claude_snap = next(s for s in result if s.source_name == "claude")
    assert claude_snap.updated_at == 2000
    assert claude_snap.windows["five_hour"].used_percentage == 20.0


def testcollapse_by_source_returns_empty_for_empty_input() -> None:
    assert collapse_by_source([]) == []


def test_render_model_marks_past_reset_as_stale() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=999,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=900)},
    )
    model = _build_render_model(snapshot, max_age=300, now=1000)
    # Age=1 (<300) so only the past-reset cause should fire.
    assert model.has_past_reset is True
    assert model.is_age_stale is False
    assert model.is_stale is True


def test_render_model_age_stale() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=500,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=2000)},
    )
    model = _build_render_model(snapshot, max_age=300, now=1000)
    # Reset is in the future so only the age cause should fire.
    assert model.is_age_stale is True
    assert model.has_past_reset is False
    assert model.is_stale is True


def test_render_model_fresh() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=950,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=2000)},
    )
    model = _build_render_model(snapshot, max_age=300, now=1000)
    assert model.is_age_stale is False
    assert model.has_past_reset is False
    assert model.is_stale is False


def test_flatten_for_template_emits_only_present_windows() -> None:
    """Format-template flat dict reflects only the windows the writer actually
    emitted. Absent windows produce no template keys -- that's the writer's
    responsibility to populate, not mngr_usage's to synthesize."""
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=900,
        windows={"five_hour": WindowSnapshot(used_percentage=42.0, resets_at=1500)},
    )
    model = _build_render_model(snapshot, max_age=300, now=1000)
    flat = _flatten_primary_for_template(model, now=1000)
    assert flat["source"] == "claude"
    assert flat["five_hour.used_percentage"] == "42.00"
    assert flat["five_hour.resets_at"] == "1500"
    assert flat["five_hour.seconds_until_reset"] == "500"
    assert flat["five_hour.is_present"] == "true"
    # seven_day was not emitted by the writer, so no seven_day.* keys exist.
    assert "seven_day.is_present" not in flat
    assert "seven_day.used_percentage" not in flat


# =============================================================================
# CLI integration: plant events.jsonl files under the test's host_dir
# =============================================================================


@pytest.fixture
def cli_profile_dir(temp_host_dir: Path, temp_profile_dir: Path) -> Path:
    """Pin the CLI's auto-resolved profile_dir so writes via temp_host_dir reach the CLI."""
    config_path = temp_host_dir / ROOT_CONFIG_FILENAME
    config_path.write_text(f'profile = "{temp_profile_dir.name}"\n')
    return temp_profile_dir


@pytest.fixture
def cli_test_agent(local_host: Host, tmp_path: Path) -> AgentInterface:
    """Register a real local agent (not started) so ``list_agents`` finds it.

    Returns the registered agent; tests can plant events into its state dir
    at ``get_agent_state_dir_path(local_host.host_dir, agent.id) / "events" / ...``.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return local_host.create_agent_state(
        work_dir_path=work_dir,
        options=CreateAgentOptions(
            name=AgentName("usage-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )


def _plant_event_for_agent(
    local_host: Host, agent: AgentInterface, event: dict[str, Any], source: str = "claude"
) -> None:
    """Plant an event into the agent's events file at the conventional path."""
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
    events_file = state_dir / "events" / source / "rate_limits" / "events.jsonl"
    _write_event(events_file, event)


@pytest.mark.tmux
def test_usage_command_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-1",
            # Timestamp in the future so the snapshot won't be stale-by-age in the test
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {
                "five_hour": {"used_percentage": 73.4, "resets_at": 9_999_999_999_999, "label": "5h"},
            },
        },
    )
    result = cli_runner.invoke(usage, ["--max-age", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Writer emitted label="5h", so the line uses "5h:" rather than the literal key.
    assert "5h:" in result.output
    assert "73% used" in result.output


@pytest.mark.tmux
def test_usage_command_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-1",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {"five_hour": {"used_percentage": 12.3, "resets_at": 9_999_999_999_999}},
        },
    )
    result = cli_runner.invoke(
        usage, ["--format", "json", "--max-age", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["sources"][0]["source"] == "claude"
    assert payload["sources"][0]["five_hour"]["used_percentage"] == 12.3
    assert payload["sources"][0]["five_hour"]["is_present"] is True
    # No window_seconds emitted in this event, so derived elapsed_* fields are None.
    assert payload["sources"][0]["five_hour"]["window_seconds"] is None
    assert payload["sources"][0]["five_hour"]["elapsed_seconds"] is None
    assert payload["sources"][0]["five_hour"]["elapsed_percentage"] is None
    # seven_day was not emitted by the writer, so it doesn't appear in the JSON either.
    assert "seven_day" not in payload["sources"][0]


@pytest.mark.tmux
def test_usage_command_json_surfaces_elapsed_when_window_seconds_present(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """When the writer emits window_seconds, the JSON output exposes elapsed_seconds + elapsed_percentage.

    Anchors `resets_at` 5400s into the future of a 18000s window so 70% has elapsed,
    independent of when the test runs.
    """
    now_s = int(datetime.now(timezone.utc).timestamp())
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-1",
            # Use a fresh ISO timestamp so the snapshot isn't age-stale.
            "timestamp": datetime.fromtimestamp(now_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
            "rate_limits": {
                "five_hour": {
                    "used_percentage": 12.3,
                    "resets_at": now_s + 5400,
                    "window_seconds": 18000,
                },
            },
        },
    )
    result = cli_runner.invoke(
        usage, ["--format", "json", "--max-age", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    five_hour = payload["sources"][0]["five_hour"]
    assert five_hour["window_seconds"] == 18000
    # Approximately 18000 - 5400 = 12600 seconds elapsed = ~70% of the window.
    # The CLI invocation uses its own wall-clock for `now`, which may have advanced
    # a few seconds since the event timestamp; allow that drift in the assertion.
    assert 12595 <= five_hour["elapsed_seconds"] <= 12605
    assert abs(five_hour["elapsed_percentage"] - 70.0) < 0.1


@pytest.mark.tmux
def test_usage_command_format_template(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-1",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {
                "five_hour": {"used_percentage": 88.0, "resets_at": 9_999_999_999_999},
                "seven_day": {"used_percentage": 44.0, "resets_at": 9_999_999_999_999},
            },
        },
    )
    result = cli_runner.invoke(
        usage,
        ["--format", "5h:{five_hour.used_percentage}/7d:{seven_day.used_percentage}", "--max-age", "300"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "5h:88.00/7d:44.00" in result.output


def test_usage_command_no_data_when_no_events(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """No agents on the host means no events files; render the no-data hint."""
    result = cli_runner.invoke(usage, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "No usage data yet" in result.output


@pytest.mark.tmux
def test_usage_command_picks_freshest_across_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    tmp_path: Path,
    cli_profile_dir: Path,
) -> None:
    """Two agents, two events, the most-recent timestamp wins."""
    work_dir_old = tmp_path / "work-old"
    work_dir_old.mkdir()
    agent_old = local_host.create_agent_state(
        work_dir_path=work_dir_old,
        options=CreateAgentOptions(
            name=AgentName("usage-test-old"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    work_dir_new = tmp_path / "work-new"
    work_dir_new.mkdir()
    agent_new = local_host.create_agent_state(
        work_dir_path=work_dir_new,
        options=CreateAgentOptions(
            name=AgentName("usage-test-new"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    _plant_event_for_agent(
        local_host,
        agent_old,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-old",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": 9_999_999_999_999}},
        },
    )
    _plant_event_for_agent(
        local_host,
        agent_new,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-new",
            "timestamp": "2056-05-08T11:00:00.000000000Z",
            "rate_limits": {"five_hour": {"used_percentage": 99.0, "resets_at": 9_999_999_999_999}},
        },
    )
    result = cli_runner.invoke(
        usage, ["--format", "json", "--max-age", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    # Both events share source_name="claude" since they live under .../events/claude/...
    # collapse_by_source keeps only the freshest per source, so we see exactly one
    # entry and its data is the newer event's.
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["source"] == "claude"
    assert payload["sources"][0]["five_hour"]["used_percentage"] == 99.0


@pytest.mark.tmux
def test_usage_command_uses_reset_specific_warning_when_window_just_reset(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """Regression: when a snapshot is fresh but a window already reset, the
    warning should call out the reset specifically (not say "snapshot last
    updated now ago"). The age-based warning fires only when the snapshot
    itself is stale by age."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-fresh",
            "timestamp": now_iso,
            "rate_limits": {
                "five_hour": {"used_percentage": 37.0, "resets_at": 1000, "label": "5h"},
            },
        },
    )
    result = cli_runner.invoke(usage, ["--max-age", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Age warning is gone (snapshot was just written).
    assert "snapshot last updated" not in result.output
    assert "now ago" not in result.output
    # Reset-specific warning fires instead.
    assert "a window already reset" in result.output


@pytest.mark.tmux
def test_usage_wait_matches_when_predicate_already_true(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """End-to-end: planted snapshot already satisfies the predicate -> exit 0 on first poll."""
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-1",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {
                "five_hour": {"used_percentage": 12.0, "resets_at": 9_999_999_999_999, "window_seconds": 18000},
            },
        },
    )
    result = cli_runner.invoke(
        usage,
        ["wait", "--until", "five_hour.used_percentage < 50", "--interval", "1s", "--timeout", "5s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "Matched on source" in result.output or "matched" in result.output.lower()


@pytest.mark.tmux
def test_usage_wait_times_out_when_predicate_never_satisfied(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """End-to-end: predicate always false -> exit 2 (timeout) after --timeout passes."""
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-1",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {
                "five_hour": {"used_percentage": 90.0, "resets_at": 9_999_999_999_999, "window_seconds": 18000},
            },
        },
    )
    result = cli_runner.invoke(
        usage,
        ["wait", "--until", "five_hour.used_percentage < 50", "--interval", "1s", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # Exit code 2 == EXIT_CODE_TIMEOUT from mngr.cli.exit_codes; matches `mngr wait`.
    assert result.exit_code == 2, result.output
    assert "Timed out" in result.output


def test_usage_wait_rejects_group_level_options_when_subcommand_invoked(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """Group-level options like `--local` placed before the subcommand are silently
    ignored by Click's early-return. We surface a UserInputError instead so the user
    sees their flag is in the wrong position."""
    result = cli_runner.invoke(
        usage,
        ["--local", "wait", "--until", "true", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    # The error names the offending flag and the corrective placement.
    assert "--local" in result.output
    assert "wait" in result.output


def test_usage_wait_accepts_subcommand_level_options(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """Sanity: putting the same flag after the subcommand is the supported form
    and reaches the wait body (here it times out since no matching agent exists)."""
    result = cli_runner.invoke(
        usage,
        ["wait", "--until", "true", "--local", "--interval", "1s", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # `--until 'true'` would normally match instantly, but with no agents present
    # there are no snapshots to evaluate against, so the wait times out (exit 2).
    assert result.exit_code in (0, 2), result.output


def test_usage_wait_rejects_invalid_cel(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """Invalid CEL must fail fast with a clear error rather than time out."""
    result = cli_runner.invoke(
        usage,
        ["wait", "--until", "this is not a valid cel expression {[", "--interval", "1s", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # MngrError bubbles up as a non-zero exit; the user-visible signal is the
    # "Invalid include filter" message.
    assert result.exit_code != 0
    assert "Invalid" in result.output or "invalid" in result.output.lower()


@pytest.mark.tmux
def test_usage_command_human_format_multi_source(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    tmp_path: Path,
    cli_profile_dir: Path,
) -> None:
    """When two distinct sources contribute, render each as its own [source] section."""
    work_dir_a = tmp_path / "work-a"
    work_dir_a.mkdir()
    agent_a = local_host.create_agent_state(
        work_dir_path=work_dir_a,
        options=CreateAgentOptions(
            name=AgentName("usage-test-claude"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    work_dir_b = tmp_path / "work-b"
    work_dir_b.mkdir()
    agent_b = local_host.create_agent_state(
        work_dir_path=work_dir_b,
        options=CreateAgentOptions(
            name=AgentName("usage-test-opencode"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    _plant_event_for_agent(
        local_host,
        agent_a,
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-claude",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {"five_hour": {"used_percentage": 11.0, "resets_at": 9_999_999_999_999}},
        },
        source="claude",
    )
    _plant_event_for_agent(
        local_host,
        agent_b,
        {
            "source": "opencode/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-opencode",
            "timestamp": "2056-05-08T11:00:00.000000000Z",
            "rate_limits": {"five_hour": {"used_percentage": 22.0, "resets_at": 9_999_999_999_999}},
        },
        source="opencode",
    )
    result = cli_runner.invoke(usage, ["--max-age", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Both source headers present
    assert "[claude]" in result.output
    assert "[opencode]" in result.output
    # Both percentages rendered (somewhere)
    assert "11% used" in result.output
    assert "22% used" in result.output
    # Freshest first: opencode's section should appear before claude's
    assert result.output.index("[opencode]") < result.output.index("[claude]")
