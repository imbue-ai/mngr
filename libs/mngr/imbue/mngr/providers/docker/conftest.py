import hashlib
import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mngr.providers.docker.testing import remove_all_containers_by_prefix
from imbue.mngr.utils.testing import generate_test_environment_name
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import run_mngr_subprocess


def write_fake_docker_context(config_dir: Path, context_name: str, host_url: str) -> None:
    """Write a fake Docker config and context metadata into *config_dir*.

    Used by the ``fake_docker_config`` fixture to set up a deterministic
    Docker context for tests that exercise ``_get_docker_context_host``.
    """
    (config_dir / "config.json").write_text(json.dumps({"currentContext": context_name}))
    if context_name == "default":
        return
    ctx_id = hashlib.sha256(context_name.encode()).hexdigest()
    meta_dir = config_dir / "contexts" / "meta" / ctx_id
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "Name": context_name,
        "Metadata": {},
        "Endpoints": {"docker": {"Host": host_url, "SkipTLSVerify": False}},
    }
    (meta_dir / "meta.json").write_text(json.dumps(meta))


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
