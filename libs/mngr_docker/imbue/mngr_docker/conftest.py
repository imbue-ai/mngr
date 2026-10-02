import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Final

import pytest
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.utils.testing import generate_test_environment_name
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_docker.instance import DockerProviderInstance
from imbue.mngr_docker.leaked_container_sweep import worker_docker_state_prefixes
from imbue.mngr_docker.testing import make_docker_provider_with_cleanup
from imbue.mngr_docker.testing import remove_all_containers_by_prefix_via_cli


@pytest.fixture
def docker_provider(temp_mngr_ctx: MngrContext) -> Generator[DockerProviderInstance, None, None]:
    yield from make_docker_provider_with_cleanup(temp_mngr_ctx)


@pytest.fixture
def docker_subprocess_env(tmp_path: Path) -> Generator[dict[str, str], None, None]:
    """Create a subprocess test environment for Docker tests.

    On teardown, destroys all agents created by this test via ``mngr destroy``,
    then force-removes ALL Docker containers (and volumes) whose name starts
    with the test prefix.  This catches both host containers and state
    containers even when ``mngr destroy`` fails or the test is interrupted.

    Cleanup uses the docker CLI (not the in-process SDK): the resource guard
    keeps ``_PYTEST_GUARD_PHASE`` at "call" through teardown, and these
    subprocess tests are marked ``docker`` but not ``docker_sdk``, so an
    SDK-based cleanup would be guard-blocked and silently leak the state
    container.
    """
    host_dir = tmp_path / "docker-test-hosts"
    host_dir.mkdir()
    prefix = f"{generate_test_environment_name()}-"
    env = get_subprocess_test_env(
        root_name="mngr-docker-test",
        prefix=prefix,
        host_dir=host_dir,
    )
    # Register the prefix so the session-end safety net can attribute any
    # leaked state container (named "<prefix>docker-state-<user_id>") to this
    # worker and fail the suite if the teardown below fails to remove it.
    worker_docker_state_prefixes.append(prefix)
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

    # Force-remove ALL Docker containers (and volumes) whose name starts with
    # the test prefix.  Even if ``mngr destroy`` missed a container (e.g. the
    # test was interrupted, or destroy failed silently), we still remove it
    # here.  Uses the docker CLI because the SDK is guard-blocked through
    # teardown for these docker-but-not-docker_sdk tests (see docstring).
    remove_all_containers_by_prefix_via_cli(prefix)


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


class _DockerdStartupError(MngrError):
    """Raised when the release-test session fixture cannot bring dockerd up."""


_DOCKERD_STARTUP_ATTEMPTS: Final[int] = 3


@pytest.fixture(scope="session", autouse=True)
def _ensure_dockerd_for_release() -> None:
    """Start the Docker daemon if running inside a release test sandbox.

    The Dockerfile.release installs /start-dockerd.sh. The sandbox CMD also
    runs it at launch, but offload overrides the entrypoint, so this session
    fixture is how dockerd actually comes up for release tests.

    start-dockerd.sh is idempotent and polls `docker info` internally until
    the daemon is ready. On gVisor the first attempt can flake (iptables
    setup, IPv6 disable, dockerd bind race), so we retry up to
    _DOCKERD_STARTUP_ATTEMPTS times and verify /var/run/docker.sock exists
    before returning. If we still cannot bring dockerd up, we raise --
    otherwise every docker/docker_sdk test in the session would fail with
    an opaque FileNotFoundError on the socket.
    """
    start_script = Path("/start-dockerd.sh")
    if not start_script.exists():
        return

    docker_sock = Path("/var/run/docker.sock")
    if docker_sock.exists():
        # dockerd already running -- typically started by the Dockerfile.release
        # CMD at sandbox launch. Skip the startup script entirely. Some Modal
        # sandboxes have a read-only /etc/resolv.conf, and running the script
        # when dockerd is already up would otherwise fail there for no reason.
        return

    last_result = None
    for attempt in range(_DOCKERD_STARTUP_ATTEMPTS):
        cg = ConcurrencyGroup(name=f"ensure-dockerd-{attempt}")
        with cg:
            last_result = cg.run_process_to_completion(
                [str(start_script)],
                is_checked_after=False,
            )
        if last_result.returncode == 0 and docker_sock.exists():
            logger.info("[_ensure_dockerd_for_release] dockerd ready on attempt {}", attempt + 1)
            return
        logger.warning(
            "[_ensure_dockerd_for_release] attempt {} failed: returncode={} socket_exists={}\nstdout: {}\nstderr: {}",
            attempt + 1,
            last_result.returncode,
            docker_sock.exists(),
            last_result.stdout,
            last_result.stderr,
        )

    assert last_result is not None
    raise _DockerdStartupError(
        f"Failed to start dockerd after {_DOCKERD_STARTUP_ATTEMPTS} attempts. "
        f"Last returncode={last_result.returncode}, "
        f"socket_exists={docker_sock.exists()}. "
        f"stdout={last_result.stdout!r} "
        f"stderr={last_result.stderr!r}"
    )
