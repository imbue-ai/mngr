"""Unit tests for mngr_usage.cli (agent-agnostic CLI + walk-by-convention discovery)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.config.consts import ROOT_CONFIG_FILENAME
from imbue.mngr_usage.cli import _build_render_model
from imbue.mngr_usage.cli import _collapse_by_source
from imbue.mngr_usage.cli import _flatten_primary_for_template
from imbue.mngr_usage.cli import _format_duration
from imbue.mngr_usage.cli import _format_human_line
from imbue.mngr_usage.cli import _format_reset_phrase
from imbue.mngr_usage.cli import _gather_snapshots
from imbue.mngr_usage.cli import _parse_max_age
from imbue.mngr_usage.cli import _pick_freshest
from imbue.mngr_usage.cli import _read_last_event
from imbue.mngr_usage.cli import _snapshot_from_event
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
    with pytest.raises(click.UsageError):
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


def test_read_last_event_picks_last_valid_line(tmp_path: Path) -> None:
    """A truncated/garbage trailing line is skipped; the previous valid line wins."""
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        json.dumps(_make_event("2026-05-08T10:00:00.000000000Z"))
        + "\n"
        + json.dumps(_make_event("2026-05-08T11:00:00.000000000Z"))
        + "\n"
        + "{not valid json"
    )
    event = _read_last_event(events_file)
    assert event is not None
    assert event["timestamp"] == "2026-05-08T11:00:00.000000000Z"


def test_read_last_event_returns_none_when_no_valid_lines(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    events_file.write_text("garbage\nstill garbage\n")
    assert _read_last_event(events_file) is None


def test_read_last_event_returns_none_when_missing(tmp_path: Path) -> None:
    assert _read_last_event(tmp_path / "missing.jsonl") is None


def test_snapshot_from_event_drops_events_without_rate_limits() -> None:
    """An event line without a rate_limits field can't make a useful snapshot."""
    event = {
        "source": "claude/rate_limits",
        "timestamp": "2026-05-08T10:00:00.000000000Z",
        "type": "rate_limit_snapshot",
        # no rate_limits field
    }
    assert _snapshot_from_event(event, source_name="claude") is None


def test_snapshot_from_event_drops_unparseable_timestamps() -> None:
    event = _make_event("not-a-timestamp")
    assert _snapshot_from_event(event, source_name="claude") is None


def test_snapshot_from_event_round_trips_window_data() -> None:
    event = _make_event("2026-05-08T10:00:00.000000000Z", used_percentage=42.5, resets_at=1778280000)
    snap = _snapshot_from_event(event, source_name="claude")
    assert snap is not None
    assert snap.source_name == "claude"
    assert snap.windows["five_hour"].used_percentage == 42.5
    assert snap.windows["five_hour"].resets_at == 1778280000


def test_gather_snapshots_walks_per_agent_event_files(tmp_path: Path) -> None:
    """Should find rate_limits events under agents/<id>/events/<source>/rate_limits/events.jsonl."""
    host_dir = tmp_path / "host"
    # Two agents, each with a rate_limits events file at the conventional path.
    _write_event(
        host_dir / "agents" / "agent-aaa" / "events" / "claude" / "rate_limits" / "events.jsonl",
        _make_event("2026-05-08T10:00:00.000000000Z", used_percentage=10.0),
    )
    _write_event(
        host_dir / "agents" / "agent-bbb" / "events" / "claude" / "rate_limits" / "events.jsonl",
        _make_event("2026-05-08T11:00:00.000000000Z", used_percentage=99.0),
    )
    snapshots = _gather_snapshots(host_dir)
    assert len(snapshots) == 2
    # Both have source_name="claude" since both events files live under events/claude/
    assert {s.source_name for s in snapshots} == {"claude"}


def test_gather_snapshots_handles_missing_dirs(tmp_path: Path) -> None:
    """Missing agents dir, missing events dir, missing rate_limits subdir: all yield nothing."""
    assert _gather_snapshots(tmp_path / "no_such_dir") == []

    host_dir = tmp_path / "host"
    (host_dir / "agents").mkdir(parents=True)
    # No agents
    assert _gather_snapshots(host_dir) == []

    # Agent has no events dir
    (host_dir / "agents" / "agent-aaa").mkdir()
    assert _gather_snapshots(host_dir) == []

    # Events dir is empty
    (host_dir / "agents" / "agent-aaa" / "events").mkdir()
    assert _gather_snapshots(host_dir) == []


def test_gather_snapshots_picks_up_alternate_source_segments(tmp_path: Path) -> None:
    """The segment after events/ is the source name -- non-claude sources work too."""
    host_dir = tmp_path / "host"
    _write_event(
        host_dir / "agents" / "agent-x" / "events" / "opencode" / "rate_limits" / "events.jsonl",
        _make_event("2026-05-08T10:00:00.000000000Z"),
    )
    snapshots = _gather_snapshots(host_dir)
    assert len(snapshots) == 1
    assert snapshots[0].source_name == "opencode"


def test_gather_snapshots_expands_tilde_in_host_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``~``-prefixed host_dir must be expanded before walking the filesystem.

    Regression: mngr's pydantic default for ``default_host_dir`` is the literal
    ``Path("~/.mngr")`` (unexpanded). Without ``expanduser()``, a clean shell
    with no ``MNGR_HOST_DIR`` env override would walk a non-existent
    ``~/.mngr/agents`` and silently report no usage data.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_event(
        tmp_path / ".mngr" / "agents" / "agent-aaa" / "events" / "claude" / "rate_limits" / "events.jsonl",
        _make_event("2026-05-08T10:00:00.000000000Z", used_percentage=42.0),
    )
    snapshots = _gather_snapshots(Path("~/.mngr"))
    assert len(snapshots) == 1
    assert snapshots[0].source_name == "claude"


# =============================================================================
# Snapshot picking + render model
# =============================================================================


def _snap(name: str = "x", at: int = 1000, percentage: float | None = 50.0) -> UsageSnapshot:
    return UsageSnapshot(
        source_name=name,
        updated_at=at,
        windows={"five_hour": WindowSnapshot(used_percentage=percentage, resets_at=at + 3600)},
    )


def test_pick_freshest_returns_none_for_empty() -> None:
    assert _pick_freshest([]) is None


def test_pick_freshest_picks_largest_updated_at() -> None:
    a = _snap(name="a", at=1000)
    b = _snap(name="b", at=2000)
    assert _pick_freshest([a, b]) == b
    assert _pick_freshest([b, a]) == b


def test_pick_freshest_tiebreaks_by_source_name() -> None:
    a = _snap(name="a", at=1000)
    z = _snap(name="z", at=1000)
    assert _pick_freshest([a, z]) == z


def test_collapse_by_source_picks_freshest_per_source() -> None:
    """Multiple agents writing to the same source should collapse to the freshest."""
    older_claude = _snap(name="claude", at=1000, percentage=10.0)
    newer_claude = _snap(name="claude", at=2000, percentage=20.0)
    only_opencode = _snap(name="opencode", at=1500, percentage=30.0)
    result = _collapse_by_source([older_claude, newer_claude, only_opencode])
    assert {s.source_name for s in result} == {"claude", "opencode"}
    claude_snap = next(s for s in result if s.source_name == "claude")
    assert claude_snap.updated_at == 2000
    assert claude_snap.windows["five_hour"].used_percentage == 20.0


def test_collapse_by_source_returns_empty_for_empty_input() -> None:
    assert _collapse_by_source([]) == []


def test_render_model_marks_past_reset_as_stale() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=999,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=900)},
    )
    model = _build_render_model(snapshot, max_age=300, now=1000)
    assert model.is_stale is True


def test_render_model_age_stale() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=500,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=2000)},
    )
    model = _build_render_model(snapshot, max_age=300, now=1000)
    assert model.is_stale is True


def test_render_model_fresh() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=950,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=2000)},
    )
    model = _build_render_model(snapshot, max_age=300, now=1000)
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


def _plant_event(host_dir: Path, agent_id: str, event: dict[str, Any], source: str = "claude") -> None:
    events_file = host_dir / "agents" / agent_id / "events" / source / "rate_limits" / "events.jsonl"
    _write_event(events_file, event)


def test_usage_command_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    cli_profile_dir: Path,
) -> None:
    _plant_event(
        temp_host_dir,
        "agent-aaa",
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


def test_usage_command_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    cli_profile_dir: Path,
) -> None:
    _plant_event(
        temp_host_dir,
        "agent-aaa",
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
    # seven_day was not emitted by the writer, so it doesn't appear in the JSON either.
    assert "seven_day" not in payload["sources"][0]


def test_usage_command_format_template(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    cli_profile_dir: Path,
) -> None:
    _plant_event(
        temp_host_dir,
        "agent-aaa",
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
    temp_host_dir: Path,
    cli_profile_dir: Path,
) -> None:
    """No agents on the host means no events files; render the no-data hint."""
    result = cli_runner.invoke(usage, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "No usage data yet" in result.output


def test_usage_command_picks_freshest_across_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    cli_profile_dir: Path,
) -> None:
    """Two agents, two events, the most-recent timestamp wins."""
    _plant_event(
        temp_host_dir,
        "agent-old",
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-old",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": 9_999_999_999_999}},
        },
    )
    _plant_event(
        temp_host_dir,
        "agent-new",
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
    # _collapse_by_source keeps only the freshest per source, so we see exactly one
    # entry and its data is the newer event's.
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["source"] == "claude"
    assert payload["sources"][0]["five_hour"]["used_percentage"] == 99.0


def test_usage_command_human_format_multi_source(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    cli_profile_dir: Path,
) -> None:
    """When two distinct sources contribute, render each as its own [source] section."""
    _plant_event(
        temp_host_dir,
        "agent-aaa",
        {
            "source": "claude/rate_limits",
            "type": "rate_limit_snapshot",
            "event_id": "evt-claude",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "rate_limits": {"five_hour": {"used_percentage": 11.0, "resets_at": 9_999_999_999_999}},
        },
        source="claude",
    )
    _plant_event(
        temp_host_dir,
        "agent-bbb",
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
