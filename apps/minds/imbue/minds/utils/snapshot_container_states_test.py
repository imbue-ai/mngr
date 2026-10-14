import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from imbue.minds.errors import SnapshotContainerStatesError
from imbue.minds.utils.snapshot_container_states import DockerContainerState
from imbue.minds.utils.snapshot_container_states import read_snapshot_container_states
from imbue.minds.utils.snapshot_container_states import record_snapshot_container_states


def test_record_snapshot_container_states_reads_and_publishes_the_first_reading(tmp_path: Path) -> None:
    path = tmp_path / "states.json"

    record_snapshot_container_states(path, lambda: {"minds-ci-snapshot-forever-1a2b3c4d": "exited"})

    assert read_snapshot_container_states(path) == {"minds-ci-snapshot-forever-1a2b3c4d": DockerContainerState.EXITED}


def test_record_snapshot_container_states_keeps_the_first_reading_after_containers_start(tmp_path: Path) -> None:
    """A later batch in the same reused sandbox must not overwrite the resumed-into reading.

    This is the flake MIND-273 was: offload reuses a snapshot sandbox across
    batches, and a batch that ran a resume test leaves the snapshot's containers
    ``running``, so any reading taken after it reports ``running`` for a
    snapshot that really did capture them stopped.
    """
    path = tmp_path / "states.json"
    record_snapshot_container_states(path, lambda: {"minds-ci-snapshot-forever-1a2b3c4d": "exited"})

    record_snapshot_container_states(path, lambda: {"minds-ci-snapshot-forever-1a2b3c4d": "running"})

    assert read_snapshot_container_states(path) == {"minds-ci-snapshot-forever-1a2b3c4d": DockerContainerState.EXITED}


def test_record_snapshot_container_states_does_not_read_once_published(tmp_path: Path) -> None:
    """The later caller must not even look at the daemon, whose state is no longer the snapshot's."""
    path = tmp_path / "states.json"
    record_snapshot_container_states(path, lambda: {"minds-ci-snapshot-forever-1a2b3c4d": "exited"})
    reads: list[str] = []

    def read_states_and_record_the_attempt() -> Mapping[str, str]:
        reads.append("read")
        return {"minds-ci-snapshot-forever-1a2b3c4d": "running"}

    record_snapshot_container_states(path, read_states_and_record_the_attempt)

    assert reads == []


def test_record_snapshot_container_states_publishes_an_empty_reading(tmp_path: Path) -> None:
    """An empty reading is itself evidence (dockerd restored no containers), so it must publish."""
    path = tmp_path / "states.json"

    record_snapshot_container_states(path, dict)

    assert read_snapshot_container_states(path) == {}


def test_record_snapshot_container_states_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    states_dir = tmp_path / "snapshot-container-states"
    states_dir.mkdir()
    path = states_dir / "states.json"

    record_snapshot_container_states(path, lambda: {"minds-ci-snapshot-forever-1a2b3c4d": "exited"})

    assert sorted(entry.name for entry in states_dir.iterdir()) == ["states.json"]


def test_read_snapshot_container_states_raises_when_nothing_was_recorded(tmp_path: Path) -> None:
    path = tmp_path / "states.json"

    with pytest.raises(SnapshotContainerStatesError, match="No resumed container states were recorded"):
        read_snapshot_container_states(path)


def test_read_snapshot_container_states_raises_on_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "states.json"
    path.write_text("{not json")

    with pytest.raises(SnapshotContainerStatesError, match="not valid JSON"):
        read_snapshot_container_states(path)


def test_read_snapshot_container_states_raises_when_the_json_is_not_a_name_to_state_mapping(tmp_path: Path) -> None:
    path = tmp_path / "states.json"
    path.write_text(json.dumps(["minds-ci-snapshot-forever-1a2b3c4d"]))

    with pytest.raises(SnapshotContainerStatesError, match="not a container-name to state mapping"):
        read_snapshot_container_states(path)


@pytest.mark.parametrize("state", list(DockerContainerState))
def test_read_snapshot_container_states_resolves_every_docker_state(
    state: DockerContainerState, tmp_path: Path
) -> None:
    path = tmp_path / "states.json"
    path.write_text(json.dumps({"minds-ci-snapshot-forever-1a2b3c4d": state.value}))

    assert read_snapshot_container_states(path) == {"minds-ci-snapshot-forever-1a2b3c4d": state}


def test_read_snapshot_container_states_raises_on_a_state_docker_does_not_report(tmp_path: Path) -> None:
    """An unrecognized state must fail the read, not pass as "not exited" or as a bare string."""
    path = tmp_path / "states.json"
    path.write_text(json.dumps({"minds-ci-snapshot-forever-1a2b3c4d": "hibernating"}))

    with pytest.raises(SnapshotContainerStatesError, match="'hibernating'.*is not a Docker container state"):
        read_snapshot_container_states(path)


def test_read_snapshot_container_states_raises_when_a_state_is_not_a_string(tmp_path: Path) -> None:
    path = tmp_path / "states.json"
    path.write_text(json.dumps({"minds-ci-snapshot-forever-1a2b3c4d": 7}))

    with pytest.raises(SnapshotContainerStatesError, match="is not a Docker container state"):
        read_snapshot_container_states(path)


def test_docker_container_state_values_are_dockers_own_lowercase_wire_format() -> None:
    assert sorted(state.value for state in DockerContainerState) == [
        "created",
        "dead",
        "exited",
        "paused",
        "removing",
        "restarting",
        "running",
    ]
