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
from imbue.mng.utils.testing import get_subprocess_test_env
from imbue.mng.utils.testing import run_mng_subprocess


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
        list_result = run_mng_subprocess("list", "--format", "json", env=env, timeout=30)
        if list_result.returncode == 0 and list_result.stdout.strip():
            data = json.loads(list_result.stdout)
            agents = data.get("agents", []) if isinstance(data, dict) else data
            for agent in agents:
                agent_name = agent.get("name", "") if isinstance(agent, dict) else ""
                if agent_name:
                    run_mng_subprocess("destroy", agent_name, "--force", env=env, timeout=30)
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
