import json
from pathlib import Path

from inline_snapshot import snapshot

from imbue.minds.desktop_client.create_progress_defaults_sync import HistoryMeasurements
from imbue.minds.desktop_client.create_progress_defaults_sync import apply_seed_sync
from imbue.minds.desktop_client.create_progress_defaults_sync import format_seed_sync_report
from imbue.minds.desktop_client.create_progress_defaults_sync import measurements_from_history_text
from imbue.minds.desktop_client.create_progress_defaults_sync import plan_seed_sync
from imbue.minds.desktop_client.create_progress_defaults_sync import sync_seed_file
from imbue.minds.desktop_client.create_progress_history import ShippedProgressDefaults
from imbue.minds.desktop_client.create_progress_history import format_shipped_defaults
from imbue.minds.desktop_client.create_progress_history import parse_shipped_defaults


def _history_line(scope: str, step_id: str, seconds: float | None) -> str:
    """A completed step's line, or a skipped step's when ``seconds`` is None."""
    fields: dict[str, object] = {
        "timestamp": "2026-10-17T00:00:00+00:00",
        "type": "step_completed" if seconds is not None else "step_skipped",
        "event_id": f"cpr-{scope}-{step_id}-{seconds}",
        "source": "create_progress",
        "scope": scope,
        "step_id": step_id,
    }
    if seconds is not None:
        fields["duration_seconds"] = seconds
    return json.dumps(fields)


def _measurements(by_scope: dict[str, dict[str, list[float]]]) -> HistoryMeasurements:
    """Measurements of exactly the steps listed, each of which the history mentions."""
    return HistoryMeasurements(
        by_scope=by_scope, seen_by_scope={scope: set(by_step) for scope, by_step in by_scope.items()}
    )


_DEFAULTS = ShippedProgressDefaults(
    default_step_seconds=10.0,
    seconds_by_step_id_by_scope={"create:DOCKER": {"clone_repo": 10.0, "docker.build": 30.0}},
)


def test_measurements_skip_unreadable_lines_and_group_by_scope_and_step_and_note_skipped_steps() -> None:
    text = "\n".join(
        [
            _history_line("create:DOCKER", "clone_repo", 4.0),
            "garbage",
            _history_line("create:DOCKER", "docker.build.run_apt_1a2b3c", None),
            _history_line("connection:aws", "aws.state_bucket", 2.0),
        ]
    )

    measurements = measurements_from_history_text(text)

    assert measurements.by_scope == {
        "create:DOCKER": {"clone_repo": [4.0]},
        "connection:aws": {"aws.state_bucket": [2.0]},
    }
    assert measurements.seen_by_scope == {
        "create:DOCKER": {"clone_repo", "docker.build.run_apt_1a2b3c"},
        "connection:aws": {"aws.state_bucket"},
    }


def test_plan_restates_only_seeds_that_drifted_past_the_threshold_and_adds_new_ones() -> None:
    plan = plan_seed_sync(
        _DEFAULTS,
        _measurements(
            {"create:DOCKER": {"clone_repo": [11.0, 12.0, 9.0], "docker.build": [60.0, 70.0, 65.0], "new_step": [3.0]}}
        ),
        drift_threshold=0.3,
    )

    assert [(change.step_id, change.previous_seconds, change.measured_seconds) for change in plan.changes] == snapshot(
        [("docker.build", 30.0, 65.0), ("new_step", None, 3.0)]
    )
    assert plan.unchanged_count == 1
    applied = apply_seed_sync(_DEFAULTS, plan)
    assert applied.seconds_by_step_id_by_scope["create:DOCKER"] == {
        "clone_repo": 10.0,
        "docker.build": 65.0,
        "new_step": 3.0,
    }
    assert "| `create:DOCKER` | `docker.build` | 30 | 65 |" in format_seed_sync_report(plan)


def test_plan_uses_the_median_of_the_most_recent_measurements() -> None:
    plan = plan_seed_sync(
        _DEFAULTS,
        _measurements({"create:DOCKER": {"clone_repo": [1.0, 100.0, 100.0, 100.0, 50.0, 51.0, 52.0]}}),
        drift_threshold=0.3,
    )

    assert plan.changes[0].measured_seconds == 52.0


def test_sync_seed_file_rewrites_the_file_only_when_not_checking(tmp_path: Path) -> None:
    defaults_path = tmp_path / "defaults.toml"
    defaults_path.write_text(format_shipped_defaults(_DEFAULTS))
    history_path = tmp_path / "events.jsonl"
    history_path.write_text(_history_line("create:DOCKER", "docker.build", 90.0) + "\n")

    checked = sync_seed_file(defaults_path, [history_path], 0.3, is_check_only=True)
    assert [change.step_id for change in checked.changes] == ["docker.build"]
    assert parse_shipped_defaults(defaults_path.read_text()) == _DEFAULTS

    written = sync_seed_file(defaults_path, [history_path], 0.3, is_check_only=False)
    assert written == checked
    assert parse_shipped_defaults(defaults_path.read_text()).seconds_by_step_id_by_scope["create:DOCKER"] == {
        "clone_repo": 10.0,
        "docker.build": 90.0,
    }
    assert format_seed_sync_report(sync_seed_file(defaults_path, [history_path], 0.3, is_check_only=True)).startswith(
        "No progress seed moved"
    )


def test_plan_never_restates_a_seed_below_one_second() -> None:
    plan = plan_seed_sync(_DEFAULTS, _measurements({"create:DOCKER": {"clone_repo": [0.0, 0.02, 0.0]}}), 0.3)

    assert [(change.step_id, change.measured_seconds) for change in plan.changes] == [("clone_repo", 1.0)]


_STAGED_DEFAULTS = ShippedProgressDefaults(
    default_step_seconds=10.0,
    seconds_by_step_id_by_scope={
        "create:DOCKER": {
            "docker.build": 400.0,
            "docker.build.run_apt_1a2b3c": 110.0,
            "docker.build.run_old_9f9f9f": 40.0,
            "docker.build.export": 200.0,
            "setting_up_host": 4.0,
            "setting_up_host.inner.command_0": 3.0,
            "setting_up_host.inner.command_1": 2.0,
            "bootstrap.sync_venv": 20.0,
        }
    },
)


def test_plan_drops_a_stage_the_measured_build_no_longer_has_but_keeps_one_that_was_only_cached() -> None:
    history = "\n".join(
        [
            _history_line("create:DOCKER", "docker.build", 380.0),
            _history_line("create:DOCKER", "docker.build.run_apt_1a2b3c", None),
            _history_line("create:DOCKER", "docker.build.export", 190.0),
            _history_line("create:DOCKER", "docker.build.copy_new_4e4e4e", 2.0),
            _history_line("create:DOCKER", "setting_up_host", 4.0),
            _history_line("create:DOCKER", "setting_up_host.inner.command_0", 3.0),
        ]
    )

    plan = plan_seed_sync(_STAGED_DEFAULTS, measurements_from_history_text(history), drift_threshold=0.3)

    # The cached stage stays (the build still has it); the stage the history
    # never mentions goes, as does the second post-host-create command (its
    # nearest measured ancestor by id is setting_up_host, past the `inner`
    # prefix); the bootstrap seeds are not under a measured step.
    assert [(removal.step_id, removal.previous_seconds) for removal in plan.removals] == snapshot(
        [("docker.build.run_old_9f9f9f", 40.0), ("setting_up_host.inner.command_1", 2.0)]
    )
    assert [change.step_id for change in plan.changes] == ["docker.build.copy_new_4e4e4e"]
    applied = apply_seed_sync(_STAGED_DEFAULTS, plan)
    assert applied.seconds_by_step_id_by_scope["create:DOCKER"] == {
        "docker.build": 400.0,
        "docker.build.run_apt_1a2b3c": 110.0,
        "docker.build.export": 200.0,
        "docker.build.copy_new_4e4e4e": 2.0,
        "setting_up_host": 4.0,
        "setting_up_host.inner.command_0": 3.0,
        "bootstrap.sync_venv": 20.0,
    }
    assert "| `create:DOCKER` | `docker.build.run_old_9f9f9f` | 40 | gone |" in format_seed_sync_report(plan)


def test_plan_keeps_every_stage_when_the_measured_build_reported_none() -> None:
    plan = plan_seed_sync(
        _STAGED_DEFAULTS, measurements_from_history_text(_history_line("create:DOCKER", "docker.build", 380.0)), 0.3
    )

    assert plan.removals == ()


def test_a_sync_from_several_histories_pools_their_measurements_and_their_steps() -> None:
    first = measurements_from_history_text(_history_line("create:DOCKER", "docker.build", 300.0))
    second = measurements_from_history_text(
        "\n".join(
            [
                _history_line("create:DOCKER", "docker.build", 500.0),
                _history_line("create:DOCKER", "docker.build.export", None),
            ]
        )
    )

    pooled = first.merged_with(second)

    assert pooled.by_scope == {"create:DOCKER": {"docker.build": [300.0, 500.0]}}
    assert pooled.seen_by_scope == {"create:DOCKER": {"docker.build", "docker.build.export"}}
