"""Session-end sweep of docker containers that tests created and failed to remove.

Registered as a resource_guards session-cleanup callback by ``register_guards``,
so it runs at the end of every pytest session in which this plugin is installed,
whichever directories that session collected. Docker containers outlive the
process that made them, so a fixture teardown that crashed (or a run that was
killed) otherwise leaves a state container and its volume behind for good.
"""

import os
from datetime import datetime
from datetime import timezone
from typing import Final

import docker
import docker.errors
import docker.models.containers
import pytest
from loguru import logger

from imbue.mngr.utils.env_utils import TEST_ENV_PREFIX
from imbue.mngr.utils.env_utils import looks_like_mngr_test_container_name
from imbue.mngr_docker.instance import create_docker_client
from imbue.mngr_docker.volume import LABEL_PROVIDER
from imbue.mngr_docker.volume import STATE_CONTAINER_TYPE_LABEL
from imbue.mngr_docker.volume import STATE_CONTAINER_TYPE_VALUE

# Track the mngr prefixes under which this worker's docker fixtures may have
# created a singleton state container. Each xdist worker is a separate process,
# so this only holds prefixes from THIS worker's tests. The sweep uses it to
# attribute leaked state containers to us (and fail), as opposed to containers
# from other concurrent workers/sessions (which it can only warn-and-clean).
worker_docker_state_prefixes: list[str] = []

# The text every resource guard rejection carries. docker-py wraps the guard's
# ResourceGuardViolation in its own DockerException while fetching the server
# version, so the wrapped message is the only way to tell a rejection from an
# unreachable daemon.
_RESOURCE_GUARD_REJECTION_TEXT: Final[str] = "RESOURCE GUARD"

_STALE_DOCKER_CONTAINER_AGE_SECONDS: Final[int] = 3600

# The provider-name prefix make_docker_provider_with_cleanup gives its providers.
TEST_PROVIDER_NAME_PREFIX: Final[str] = "docker-test-"


def remove_docker_container_and_volume(
    client: docker.DockerClient,
    container: docker.models.containers.Container,
) -> None:
    """Remove a Docker container and its backing volume (if any).

    The state container's backing Docker volume has the same name as the
    container.  The container must be removed first because Docker refuses
    to remove volumes that are still mounted.

    Errors are silently ignored so that cleanup proceeds on a best-effort
    basis.
    """
    name = container.name or ""
    try:
        container.remove(force=True)
    except docker.errors.DockerException:
        pass
    if name:
        try:
            client.volumes.get(name).remove(force=True)
        except (docker.errors.NotFound, docker.errors.DockerException):
            pass


def _is_resource_guard_rejection(exc: docker.errors.DockerException) -> bool:
    return _RESOURCE_GUARD_REJECTION_TEXT in str(exc)


def _is_test_container(provider_name: str, container_name: str) -> bool:
    """Whether a container was created by a test, judged from its provider label and name.

    Three shapes: the provider name make_docker_provider_with_cleanup assigns
    (SDK-based tests), the environment name generate_test_environment_name
    assigns (subprocess tests), and the autouse per-test prefix (mngr_ followed
    by a hex UUID) that mngr_test_prefix assigns.
    """
    return (
        provider_name.startswith(TEST_PROVIDER_NAME_PREFIX)
        or container_name.startswith(TEST_ENV_PREFIX)
        or looks_like_mngr_test_container_name(container_name)
    )


def _get_stale_docker_test_containers(max_age_seconds: int) -> list[tuple[str, str]]:
    """Get Docker containers from tests that are older than max_age_seconds.

    Returns a list of (container_id, container_name) tuples for containers
    (both state containers and host containers) that appear to originate
    from tests and are older than the threshold.  This catches containers
    leaked by crashed or interrupted test runs.
    """
    try:
        client = create_docker_client()
    except docker.errors.DockerException as e:
        if _is_resource_guard_rejection(e):
            logger.warning(
                "Skipped stale docker container sweep because the docker_sdk resource guard rejected it: {}", e
            )
            return []
        # Called unconditionally at session end, including in sessions with no
        # Docker daemon (e.g. offload sandboxes), so a connection failure here
        # is expected -- log at debug only.
        logger.debug("Skipped stale docker container sweep (Docker unavailable): {}", e)
        return []

    try:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [LABEL_PROVIDER],
            },
        )
    except docker.errors.DockerException as e:
        logger.warning("Failed to list Docker containers during stale-container sweep: {}", e)
        client.close()
        return []

    now = datetime.now(timezone.utc)
    stale: list[tuple[str, str]] = []

    for container in containers:
        labels = container.labels or {}
        if not _is_test_container(labels.get(LABEL_PROVIDER, ""), container.name or ""):
            continue

        try:
            container.reload()
            created_str = container.attrs.get("Created", "")
            if not created_str:
                continue
            # Docker returns ISO format with nanosecond precision
            created_str = created_str.split(".")[0] + "+00:00"
            created = datetime.fromisoformat(created_str)
            age_seconds = (now - created).total_seconds()
            if age_seconds > max_age_seconds:
                stale.append((container.id, container.name or ""))
        except (ValueError, KeyError, docker.errors.DockerException):
            continue

    client.close()
    return stale


def _get_leaked_state_containers_for_prefixes(prefixes: list[str]) -> list[tuple[str, str]]:
    """Find surviving state containers whose name starts with one of *prefixes*.

    Returns (container_id, container_name) tuples for state containers (those
    carrying the state-container type label) created under any of this worker's
    registered prefixes. These are leaks we can attribute to our own fixtures,
    so the caller fails the suite for them (after cleaning them up).
    """
    if not prefixes:
        return []
    try:
        client = create_docker_client()
    except docker.errors.DockerException as e:
        if _is_resource_guard_rejection(e):
            logger.error(
                "Skipped the leaked state container check because the docker_sdk resource guard rejected it: {}", e
            )
            return []
        # We only reach here when this worker's docker fixtures ran (non-empty
        # prefixes), so the daemon was reachable during the tests. Failing to
        # connect now means we cannot verify our own cleanup -- this is
        # unexpected, so surface it loudly rather than silently skipping.
        logger.opt(exception=e).error("Failed to connect to Docker to check for leaked state containers")
        return []

    try:
        containers = client.containers.list(
            all=True,
            filters={"label": [f"{STATE_CONTAINER_TYPE_LABEL}={STATE_CONTAINER_TYPE_VALUE}"]},
        )
    except docker.errors.DockerException as e:
        logger.opt(exception=e).error("Failed to list Docker containers while checking for leaked state containers")
        return []
    finally:
        client.close()

    leaked: list[tuple[str, str]] = []
    for container in containers:
        name = container.name or ""
        if any(name.startswith(prefix) for prefix in prefixes):
            leaked.append((container.id, name))
    return leaked


def _remove_docker_containers(containers: list[tuple[str, str]]) -> None:
    """Force-remove the specified Docker containers and their backing volumes.

    Takes a list of (container_id, container_name) tuples. Uses the shared
    remove_docker_container_and_volume helper which removes the container
    first, then removes the backing Docker volume (same name as the container).
    """
    if not containers:
        return

    try:
        client = create_docker_client()
    except docker.errors.DockerException:
        return

    try:
        for container_id, _name in containers:
            try:
                container = client.containers.get(container_id)
                remove_docker_container_and_volume(client, container)
            except (docker.errors.DockerException, docker.errors.NotFound):
                pass
    finally:
        client.close()


def sweep_leaked_test_containers() -> None:
    """Find and remove leaked Docker test containers once every test has finished.

    Runs from the session-finish hook, after the last test's teardown, where the
    docker_sdk resource guard's per-test environment is gone (a session fixture
    would tear down inside that teardown and be rejected unless the last test
    happened to carry ``@pytest.mark.docker_sdk``).

    Two kinds of container are handled:
    - State containers under one of this worker's registered prefixes are the
      worker's own leaks (a docker fixture failed to clean up). They fail the
      run through ``pytest.exit``, since a session-finish hook has no test
      report to fail. That reaches pytest's exit code when pytest runs without
      xdist, which is how the docker CI jobs run it; an xdist controller
      ignores a worker's exit status, so there the leak is logged and removed
      but cannot fail the run.
    - Stale test containers from other or older sessions cannot be attributed
      to this worker, so they are removed with a warning and no failure.
    """
    is_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER") is not None
    is_xdist_leader = not is_xdist_worker and os.environ.get("PYTEST_XDIST_TESTRUNUID") is not None
    if is_xdist_leader:
        return

    leaked_state_containers = _get_leaked_state_containers_for_prefixes(worker_docker_state_prefixes)
    stale_docker_containers = _get_stale_docker_test_containers(max_age_seconds=_STALE_DOCKER_CONTAINER_AGE_SECONDS)
    logger.debug(
        "Checked Docker for leaked test containers: {} from this worker's fixtures, {} stale from other sessions",
        len(leaked_state_containers),
        len(stale_docker_containers),
    )
    if stale_docker_containers:
        logger.warning(
            "Cleaning {} stale docker test container(s) from other/older sessions", len(stale_docker_containers)
        )
    _remove_docker_containers(leaked_state_containers)
    _remove_docker_containers(stale_docker_containers)

    if leaked_state_containers:
        container_lines = [f"  {name} ({cid[:12]})" for cid, name in leaked_state_containers]
        message = (
            "Leaked Docker state containers found!\n"
            "A docker fixture failed to remove its state container before completing.\n" + "\n".join(container_lines)
        )
        logger.error(message)
        pytest.exit(message, returncode=1)
