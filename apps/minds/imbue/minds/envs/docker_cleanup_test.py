from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.docker_cleanup import _is_docker_daemon_unavailable
from imbue.minds.envs.docker_cleanup import cleanup_env_state_container
from imbue.minds.envs.docker_cleanup import read_profile_user_id
from imbue.minds.envs.docker_cleanup import remove_state_container
from imbue.minds.envs.docker_cleanup import state_container_name
from imbue.minds.envs.docker_cleanup import stop_active_env_state_container
from imbue.minds.envs.docker_cleanup import stop_state_container
from imbue.minds.envs.primitives import DevEnvName


@pytest.fixture
def _root_cg() -> Iterator[ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="docker-cleanup-test-root")
    with cg:
        yield cg


def _write_profile(mngr_host_dir: Path, *, profile_id: str, user_id: str) -> None:
    mngr_host_dir.mkdir(parents=True, exist_ok=True)
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    profile_dir = mngr_host_dir / "profiles" / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "user_id").write_text(f"{user_id}\n")


def test_read_profile_user_id_returns_value(tmp_path: Path) -> None:
    mngr_host_dir = tmp_path / "mngr"
    user_id = uuid4().hex
    _write_profile(mngr_host_dir, profile_id="profile-abc", user_id=user_id)
    assert read_profile_user_id(mngr_host_dir) == user_id


def test_read_profile_user_id_missing_config_returns_none(tmp_path: Path) -> None:
    assert read_profile_user_id(tmp_path / "mngr") is None


def test_read_profile_user_id_missing_user_id_file_returns_none(tmp_path: Path) -> None:
    mngr_host_dir = tmp_path / "mngr"
    mngr_host_dir.mkdir(parents=True)
    (mngr_host_dir / "config.toml").write_text('profile = "profile-abc"\n')
    (mngr_host_dir / "profiles" / "profile-abc").mkdir(parents=True)
    # No user_id file written.
    assert read_profile_user_id(mngr_host_dir) is None


def test_read_profile_user_id_no_profile_key_returns_none(tmp_path: Path) -> None:
    mngr_host_dir = tmp_path / "mngr"
    mngr_host_dir.mkdir(parents=True)
    (mngr_host_dir / "config.toml").write_text("other = 1\n")
    assert read_profile_user_id(mngr_host_dir) is None


def test_state_container_name_shape() -> None:
    assert state_container_name("minds-staging-", "deadbeef") == "minds-staging-docker-state-deadbeef"


def test_is_docker_daemon_unavailable_detects_daemon_errors() -> None:
    assert _is_docker_daemon_unavailable("Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    assert _is_docker_daemon_unavailable("Is the docker daemon running?")
    assert _is_docker_daemon_unavailable("error during connect: ...")
    # A "no such container" message is a real (recoverable) state, not daemon-down.
    assert not _is_docker_daemon_unavailable("Error: No such container: minds-staging-docker-state-x")


def test_cleanup_env_state_container_skips_when_user_id_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _root_cg: ConcurrencyGroup
) -> None:
    # No mngr profile under HOME -> user_id can't be resolved -> no-op skip
    # (never matches a broader target, never raises).
    monkeypatch.setenv("HOME", str(tmp_path))
    cleanup_env_state_container(DevEnvName("staging"), parent_concurrency_group=_root_cg)


def test_stop_active_env_state_container_skips_when_user_id_unresolved(
    tmp_path: Path, _root_cg: ConcurrencyGroup
) -> None:
    # No mngr profile under the given host dir -> user_id can't be resolved ->
    # returns False without targeting (or stopping) anything.
    assert stop_active_env_state_container(mngr_host_dir=tmp_path / "mngr", parent_concurrency_group=_root_cg) is False


@pytest.mark.acceptance
@pytest.mark.docker
@pytest.mark.timeout(60)
def test_remove_state_container_absent_is_noop(_root_cg: ConcurrencyGroup) -> None:
    # A guaranteed-absent container name must not raise (present daemon,
    # no-such-container -> success).
    remove_state_container(
        container_name=f"minds-doesnotexist-docker-state-{uuid4().hex}",
        parent_concurrency_group=_root_cg,
    )


@pytest.mark.acceptance
@pytest.mark.docker
@pytest.mark.timeout(120)
def test_remove_state_container_removes_real_container_and_volume(_root_cg: ConcurrencyGroup) -> None:
    name = f"minds-test-docker-state-{uuid4().hex}"
    # Create a throwaway container with a same-named backing volume, mirroring
    # the mngr state-container shape.
    create = _root_cg.run_process_to_completion(
        command=[
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-v",
            f"{name}:/mngr-state",
            "alpine:latest",
            "sh",
            "-c",
            "tail -f /dev/null",
        ],
        timeout=60.0,
        is_checked_after=False,
    )
    assert create.returncode == 0, create.stderr

    remove_state_container(container_name=name, parent_concurrency_group=_root_cg)

    inspect_container = _root_cg.run_process_to_completion(
        command=["docker", "container", "inspect", name],
        timeout=60.0,
        is_checked_after=False,
    )
    assert inspect_container.returncode != 0
    inspect_volume = _root_cg.run_process_to_completion(
        command=["docker", "volume", "inspect", name],
        timeout=60.0,
        is_checked_after=False,
    )
    assert inspect_volume.returncode != 0


@pytest.mark.acceptance
@pytest.mark.docker
@pytest.mark.timeout(60)
def test_stop_state_container_absent_is_noop(_root_cg: ConcurrencyGroup) -> None:
    # A guaranteed-absent container name must not raise.
    stop_state_container(
        container_name=f"minds-doesnotexist-docker-state-{uuid4().hex}",
        parent_concurrency_group=_root_cg,
    )


@pytest.mark.acceptance
@pytest.mark.docker
@pytest.mark.timeout(120)
def test_stop_state_container_stops_without_removing(_root_cg: ConcurrencyGroup) -> None:
    name = f"minds-test-docker-state-{uuid4().hex}"
    create = _root_cg.run_process_to_completion(
        command=[
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-v",
            f"{name}:/mngr-state",
            "alpine:latest",
            "sh",
            "-c",
            "tail -f /dev/null",
        ],
        timeout=60.0,
        is_checked_after=False,
    )
    assert create.returncode == 0, create.stderr
    try:
        stop_state_container(container_name=name, parent_concurrency_group=_root_cg)

        # The container still EXISTS (not removed) but is no longer running, and
        # its backing volume is preserved.
        running = _root_cg.run_process_to_completion(
            command=["docker", "inspect", "-f", "{{.State.Running}}", name],
            timeout=60.0,
            is_checked_after=False,
        )
        assert running.returncode == 0, running.stderr
        assert running.stdout.strip() == "false"
        volume = _root_cg.run_process_to_completion(
            command=["docker", "volume", "inspect", name],
            timeout=60.0,
            is_checked_after=False,
        )
        assert volume.returncode == 0
    finally:
        _root_cg.run_process_to_completion(
            command=["docker", "rm", "-f", name],
            timeout=60.0,
            is_checked_after=False,
        )
        _root_cg.run_process_to_completion(
            command=["docker", "volume", "rm", name],
            timeout=60.0,
            is_checked_after=False,
        )
