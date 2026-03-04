import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import docker
import docker.errors
import pytest

from imbue.mng.providers.docker.testing import remove_docker_container_and_volume
from imbue.mng.providers.docker.volume import LABEL_PROVIDER
from imbue.mng.providers.docker.volume import STATE_CONTAINER_TYPE_LABEL
from imbue.mng.providers.docker.volume import STATE_CONTAINER_TYPE_VALUE
from imbue.mng.utils.testing import generate_test_environment_name
from imbue.mng.utils.testing import get_short_random_string
from imbue.mng.utils.testing import get_subprocess_test_env

pytestmark = [pytest.mark.docker, pytest.mark.acceptance, pytest.mark.rsync]


@pytest.fixture
def docker_subprocess_env(tmp_path: Path) -> Generator[dict[str, str], None, None]:
    """Create a subprocess test environment for Docker tests.

    On teardown, destroys all agents created by this test via ``mng destroy``,
    then removes the state container and its backing volume.
    """
    host_dir = tmp_path / "docker-test-hosts"
    host_dir.mkdir()
    prefix = f"{generate_test_environment_name()}-"
    env = get_subprocess_test_env(
        root_name="mng-docker-test",
        prefix=prefix,
        host_dir=host_dir,
    )
    yield env

    # Destroy all agents created during the test.
    try:
        list_result = subprocess.run(
            ["uv", "run", "mng", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if list_result.returncode == 0 and list_result.stdout.strip():
            for agent in json.loads(list_result.stdout):
                agent_name = agent.get("name", "")
                if agent_name:
                    subprocess.run(
                        ["uv", "run", "mng", "destroy", agent_name, "--force"],
                        capture_output=True,
                        timeout=30,
                        env=env,
                    )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass

    # Remove the state container and its backing volume.
    # The state container name follows the pattern {prefix}docker-state-{user_id}.
    # Since we cannot easily determine user_id, we find the container by labels.
    try:
        client = docker.from_env()
    except (docker.errors.DockerException, OSError):
        return

    try:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{STATE_CONTAINER_TYPE_LABEL}={STATE_CONTAINER_TYPE_VALUE}",
                    f"{LABEL_PROVIDER}=docker",
                ],
            },
        )
        for container in containers:
            name = container.name or ""
            if name.startswith(prefix):
                remove_docker_container_and_volume(client, container)
    except (docker.errors.DockerException, OSError):
        pass
    finally:
        client.close()


@pytest.fixture
def temp_source_dir(tmp_path: Path) -> Path:
    """Create a temporary source directory for tests."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "test.txt").write_text("test content")
    return source_dir


@pytest.mark.timeout(120)
def test_mng_create_echo_command_on_docker(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Test creating an agent with echo command on Docker using the CLI."""
    agent_name = f"test-docker-echo-{get_short_random_string()}"
    expected_output = f"hello-from-docker-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "docker",
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source-path",
            str(temp_source_dir),
            "--",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=docker_subprocess_env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.timeout(120)
def test_mng_create_with_start_args_on_docker(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Test creating a Docker host with custom CPU and memory start args."""
    agent_name = f"test-docker-start-{get_short_random_string()}"
    expected_output = f"start-test-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "docker",
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source-path",
            str(temp_source_dir),
            "-s",
            "--cpus=2",
            "-s",
            "--memory=2g",
            "--",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=docker_subprocess_env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.timeout(120)
def test_mng_create_with_tags_on_docker(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Test creating a Docker host with tags and verify they appear."""
    agent_name = f"test-docker-tags-{get_short_random_string()}"
    expected_output = f"tags-test-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "docker",
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source-path",
            str(temp_source_dir),
            "--tag",
            "env=test",
            "--",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=docker_subprocess_env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.timeout(120)
def test_mng_create_with_dockerfile_on_docker(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Test creating a Docker host using a custom Dockerfile."""
    agent_name = f"test-docker-df-{get_short_random_string()}"
    expected_output = f"dockerfile-test-{get_short_random_string()}"

    dockerfile_path = temp_source_dir / "Dockerfile"
    dockerfile_content = """\
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server \\
    tmux \\
    python3 \\
    rsync \\
    && rm -rf /var/lib/apt/lists/*

RUN echo "custom-dockerfile-marker" > /dockerfile-marker.txt
"""
    dockerfile_path.write_text(dockerfile_content)

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "docker",
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source-path",
            str(temp_source_dir),
            "-b",
            f"--file={dockerfile_path}",
            "-b",
            str(temp_source_dir),
            "--",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=docker_subprocess_env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.release
@pytest.mark.timeout(180)
def test_mng_create_stop_start_destroy_lifecycle(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Full lifecycle test: create, stop, start, destroy via CLI."""
    agent_name = f"test-docker-lifecycle-{get_short_random_string()}"

    # Create
    create_result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "generic",
            "--in",
            "docker",
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source-path",
            str(temp_source_dir),
            "--",
            "sleep 3600",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=docker_subprocess_env,
    )
    assert create_result.returncode == 0, (
        f"Create failed with stderr: {create_result.stderr}\nstdout: {create_result.stdout}"
    )

    # Stop
    stop_result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "stop",
            agent_name,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=docker_subprocess_env,
    )
    assert stop_result.returncode == 0, f"Stop failed with stderr: {stop_result.stderr}\nstdout: {stop_result.stdout}"

    # Start
    start_result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "start",
            agent_name,
            "--no-connect",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=docker_subprocess_env,
    )
    assert start_result.returncode == 0, (
        f"Start failed with stderr: {start_result.stderr}\nstdout: {start_result.stdout}"
    )

    # Destroy
    destroy_result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "destroy",
            agent_name,
            "--force",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=docker_subprocess_env,
    )
    assert destroy_result.returncode == 0, (
        f"Destroy failed with stderr: {destroy_result.stderr}\nstdout: {destroy_result.stdout}"
    )
