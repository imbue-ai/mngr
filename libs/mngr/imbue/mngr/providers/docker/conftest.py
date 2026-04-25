import json
import os
import pwd
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.testing import make_docker_provider_with_cleanup
from imbue.mngr.providers.docker.testing import remove_all_containers_by_prefix
from imbue.mngr.utils.testing import generate_test_environment_name
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import run_mngr_subprocess


def _real_user_docker_config_dir() -> str:
    """Return the user's real ~/.docker, resolved via pwd to bypass HOME overrides.

    The autouse mngr test environment isolates HOME, which hides
    ~/.docker/cli-plugins/docker-buildx (where Docker Desktop installs the
    plugin via symlink). Without buildx the CLI falls back to the deprecated
    legacy builder, which does not understand BuildKit flags like
    --progress=plain. Restoring DOCKER_CONFIG to the real ~/.docker re-exposes
    the plugin to tests that exercise `docker build`.
    """
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return str(real_home / ".docker")


@pytest.fixture
def docker_provider(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> Generator[DockerProviderInstance, None, None]:
    monkeypatch.setenv("DOCKER_CONFIG", _real_user_docker_config_dir())
    yield from make_docker_provider_with_cleanup(temp_mngr_ctx)


@pytest.fixture
def docker_subprocess_env(tmp_path: Path) -> Generator[dict[str, str], None, None]:
    """Create a subprocess test environment for Docker tests.

    On teardown, destroys all agents created by this test via ``mngr destroy``,
    then force-removes ALL Docker containers whose name starts with the test
    prefix.  This catches both host containers and state containers even when
    ``mngr destroy`` fails or the test is interrupted.
    """
    host_dir = tmp_path / "docker-test-hosts"
    host_dir.mkdir()
    prefix = f"{generate_test_environment_name()}-"
    env = get_subprocess_test_env(
        root_name="mngr-docker-test",
        prefix=prefix,
        host_dir=host_dir,
    )
    env["DOCKER_CONFIG"] = _real_user_docker_config_dir()
    yield env

    # Destroy all agents created during the test.
    try:
        list_result = run_mngr_subprocess("list", "--format", "json", env=env, timeout=30)
        if list_result.returncode == 0 and list_result.stdout.strip():
            data = json.loads(list_result.stdout)
            agents = data.get("agents", []) if isinstance(data, dict) else data
            for agent in agents:
                agent_name = agent.get("name", "") if isinstance(agent, dict) else ""
                if agent_name:
                    run_mngr_subprocess("destroy", agent_name, "--force", env=env, timeout=30)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass

    # Force-remove ALL Docker containers whose name starts with the test
    # prefix.  Even if ``mngr destroy`` missed a container (e.g. the test
    # was interrupted, or destroy failed silently), we still remove it here.
    # Subprocess tests use the default provider name "docker".
    remove_all_containers_by_prefix(prefix, provider_name="docker")


@pytest.fixture
def fake_docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DOCKER_CONFIG at a temp directory and return its path.

    Tests call ``write_fake_docker_context(path, name, url)`` to populate it.
    """
    config_dir = tmp_path / "docker-config"
    config_dir.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(config_dir))
    return config_dir


@pytest.fixture
def temp_source_dir(tmp_path: Path) -> Path:
    """Create a temporary source directory for tests."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "test.txt").write_text("test content")
    return source_dir
