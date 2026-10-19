from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.minds.desktop_client.create_progress import StepDuration
from imbue.minds.desktop_client.create_progress_history import ProgressHistoryStore
from imbue.minds.desktop_client.create_progress_history import ShippedProgressDefaults
from imbue.minds.desktop_client.create_progress_history import connection_scope
from imbue.minds.desktop_client.create_progress_history import create_scope
from imbue.minds.desktop_client.create_progress_history import format_shipped_defaults
from imbue.minds.desktop_client.create_progress_history import load_shipped_defaults
from imbue.minds.desktop_client.create_progress_history import make_progress_history_store
from imbue.minds.desktop_client.create_progress_history import parse_shipped_defaults
from imbue.minds.primitives import LaunchMode


def test_shipped_defaults_seed_every_launch_mode_and_round_trip_through_the_formatter() -> None:
    defaults = load_shipped_defaults()

    for launch_mode in LaunchMode:
        seconds = defaults.for_scope(create_scope(launch_mode)).seconds_by_step_id
        assert {"clone_repo", "mngr_create", "wait_for_ready"} <= set(seconds), launch_mode
    assert parse_shipped_defaults(format_shipped_defaults(defaults)) == defaults


def test_parse_shipped_defaults_rejects_a_malformed_table() -> None:
    with pytest.raises(ValueError, match="default_step_seconds"):
        parse_shipped_defaults('["create:DOCKER"]\nclone_repo = 1\n')
    with pytest.raises(ValueError, match="clone_repo"):
        parse_shipped_defaults('default_step_seconds = 5\n["create:DOCKER"]\nclone_repo = -1\n')


def test_store_learns_the_median_of_recent_measurements_per_scope(tmp_path: Path) -> None:
    store = ProgressHistoryStore(
        events_path=tmp_path / "events.jsonl",
        shipped_defaults=ShippedProgressDefaults(
            default_step_seconds=7.0,
            seconds_by_step_id_by_scope={"create:DOCKER": {"clone_repo": 20.0, "mngr_create": 60.0}},
        ),
    )
    docker = create_scope(LaunchMode.DOCKER)

    for seconds in (1.0, 2.0, 100.0, 3.0, 4.0, 5.0):
        store.record(docker, [StepDuration(step_id="clone_repo", seconds=seconds)])
    store.record(connection_scope("aws"), [StepDuration(step_id="clone_repo", seconds=999.0)])

    learned = store.expected_durations(docker)
    # The oldest measurement falls outside the window of five; the outlier does not move the median.
    assert learned.seconds_by_step_id == snapshot({"clone_repo": 4.0, "mngr_create": 60.0})
    assert learned.default_seconds == 7.0
    assert store.expected_durations(connection_scope("aws")).seconds_by_step_id == {"clone_repo": 999.0}


def test_store_skips_unreadable_lines_and_tolerates_a_missing_file(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    store = make_progress_history_store(tmp_path)
    assert store.events_path == tmp_path / "events" / "create_progress" / "events.jsonl"
    store = ProgressHistoryStore(events_path=events_path, shipped_defaults=load_shipped_defaults())

    assert store.expected_durations(create_scope(LaunchMode.LIMA)).seconds_by_step_id["lima.start_vm"] > 0
    events_path.write_text("not json\n")
    store.record(create_scope(LaunchMode.LIMA), [StepDuration(step_id="lima.start_vm", seconds=33.0)])

    assert store.expected_durations(create_scope(LaunchMode.LIMA)).seconds_by_step_id["lima.start_vm"] == 33.0


def test_store_records_skipped_steps_without_learning_from_them(tmp_path: Path) -> None:
    store = make_progress_history_store(tmp_path)
    scope = create_scope(LaunchMode.DOCKER)

    store.record(scope, [StepDuration(step_id="docker.build", seconds=300.0)], ["docker.build.run_apt_1a2b3c"])

    lines = store.events_path.read_text().splitlines()
    assert len(lines) == 2
    assert '"type":"step_skipped"' in lines[0] and '"duration_seconds"' not in lines[0]
    learned = store.expected_durations(scope).seconds_by_step_id
    assert learned["docker.build"] == 300.0
    assert "docker.build.run_apt_1a2b3c" not in learned
