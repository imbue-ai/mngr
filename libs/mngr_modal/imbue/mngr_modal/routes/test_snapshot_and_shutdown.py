"""Tests for the snapshot_and_shutdown Modal function.

Acceptance tests deploy the function to Modal and verify end-to-end functionality.

It is not really possible to unit test those functions (they all rely on Modal SDK calls, and cannot even be imported due to the App context requirements), so we focus on acceptance tests here.
"""

import io
import json
import subprocess
from collections.abc import Generator
from typing import Any

import httpx
import modal
import pytest
from tenacity import retry
from tenacity import retry_if_exception
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import UserId
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import register_modal_test_app
from imbue.mngr.utils.testing import register_modal_test_volume
from imbue.mngr_modal.constants import MODAL_TEST_APP_PREFIX
from imbue.mngr_modal.routes.deployment import deploy_function
from imbue.modal_proxy.direct import DirectModalInterface
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.interface import ModalInterface
from imbue.resource_guards.resource_guards import fixture_uses_resources

_MAX_MODAL_ENVIRONMENT_NAME_LENGTH = 64

# =============================================================================
# Acceptance tests (require Modal network access)
# =============================================================================


class DeploymentError(RuntimeError):
    """Raised when deploying the Modal function fails."""


class URLParseError(RuntimeError):
    """Raised when the function URL cannot be parsed from deploy output."""


class CleanupError(RuntimeError):
    """Raised when one or more Modal cleanup subprocesses exit non-zero."""


def _get_test_app_name() -> str:
    """Generate a unique test app name with the mngr-test prefix."""
    return f"{MODAL_TEST_APP_PREFIX}snapshot-{get_short_random_string()}"


def _ensure_modal_environment(modal_interface: ModalInterface, environment_name: str) -> None:
    """Create the Modal environment if it doesn't already exist."""
    try:
        modal_interface.environment_create(environment_name)
    except ModalProxyError as e:
        # Modal CLI returns this exact wording when the env already exists;
        # any other error indicates a real problem and should propagate.
        if "Can not create an environment with the same name" not in str(e):
            raise


def _is_modal_permission_propagation_error(exc: BaseException) -> bool:
    """Modal propagates per-user permissions asynchronously after env create.

    For ~3-7 seconds after `modal environment create` returns success, any
    operation in the new env fails with a read/write-access error.
    """
    message = str(exc)
    return "does not have read access" in message or "does not have write access" in message


@retry(
    retry=retry_if_exception(_is_modal_permission_propagation_error),
    stop=stop_after_attempt(8),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    reraise=True,
)
def _deploy_snapshot_function_with_propagation_retry(
    modal_interface: ModalInterface, app_name: str, environment_name: str
) -> str:
    return deploy_function("snapshot_and_shutdown", app_name, environment_name, modal_interface)


def _stop_app_and_delete_volume(app_name: str, volume_name: str, environment_name: str) -> None:
    """Stop the Modal app and delete its volume in parallel.

    Raises CleanupError listing every subprocess that exited non-zero.
    """
    with (
        subprocess.Popen(
            ["uv", "run", "modal", "app", "stop", "--env", environment_name, "--yes", app_name]
        ) as stop_process,
        subprocess.Popen(
            ["uv", "run", "modal", "volume", "delete", "--env", environment_name, volume_name, "--yes"]
        ) as delete_process,
    ):
        stop_returncode = stop_process.wait(timeout=15)
        delete_returncode = delete_process.wait(timeout=15)
    failures = []
    if stop_returncode != 0:
        failures.append(f"`modal app stop {app_name}` (env {environment_name}) exited {stop_returncode}")
    if delete_returncode != 0:
        failures.append(f"`modal volume delete {volume_name}` (env {environment_name}) exited {delete_returncode}")
    if failures:
        raise CleanupError("; ".join(failures))


def _warmup_function(url: str) -> None:
    """Send a warmup request to trigger cold start before tests run.

    This ensures the Modal container is warm and subsequent test requests
    complete within reasonable timeouts.
    """
    # Send a simple request that will fail validation but warm up the function
    # Use a longer timeout since this is the cold start
    try:
        httpx.post(url, json={}, timeout=180)
    except httpx.HTTPError:
        # Ignore errors - we just want to trigger the cold start
        pass


def _create_test_sandbox(app_name: str, environment_name: str) -> tuple[modal.Sandbox, str]:
    """Create a test sandbox within the given app.

    Creates a simple sandbox that sleeps, suitable for testing snapshot functionality.
    """
    app = modal.App.lookup(app_name, create_if_missing=True, environment_name=environment_name)
    sandbox = modal.Sandbox.create(
        app=app,
        image=modal.Image.debian_slim(),
        timeout=300,
    )
    sandbox.exec("sleep", "3600")
    return sandbox, sandbox.object_id


def _write_host_record_to_volume(app_name: str, host_id: str, environment_name: str) -> None:
    """Write a host record to the Modal volume for testing.

    Creates a minimal host record that the snapshot function can update.
    The structure matches HostRecord model with nested certified_host_data.
    """
    volume_name = f"{app_name}-state"
    register_modal_test_volume(volume_name)
    volume = modal.Volume.from_name(volume_name, create_if_missing=True, environment_name=environment_name)

    host_record = {
        "certified_host_data": {
            "host_id": host_id,
            "host_name": "test-host",
            "snapshots": [],
        },
    }

    content = json.dumps(host_record, indent=2).encode("utf-8")
    with volume.batch_upload() as batch:
        batch.put_file(io.BytesIO(content), f"/hosts/{host_id}.json")


def _read_host_record_from_volume(app_name: str, host_id: str, environment_name: str) -> dict[str, Any] | None:
    """Read a host record from the Modal volume."""
    volume_name = f"{app_name}-state"
    register_modal_test_volume(volume_name)
    volume = modal.Volume.from_name(volume_name, environment_name=environment_name)

    try:
        content = b"".join(volume.read_file(f"/hosts/{host_id}.json"))
        return json.loads(content.decode("utf-8"))
    except modal.exception.NotFoundError:
        return None


@pytest.fixture(scope="module")
@fixture_uses_resources("modal")
def deployed_snapshot_function(
    modal_test_session_env_name: str,
    modal_test_session_user_id: UserId,
    modal_test_session_cleanup: None,
) -> Generator[tuple[str, str, str], None, None]:
    """Deploy the snapshot function for testing and clean up after.

    Yields a tuple of (app_name, function_url, environment_name). Module-scoped
    so the expensive deploy + cold-start warmup runs exactly once per module
    execution. The fixture-scope resource guard authorizes the modal calls
    inside setup/teardown against the fixture's own declaration.

    Uses the session-scoped Modal env (same one threaded through
    `real_modal_provider` and `modal_subprocess_env`) so the deployed app +
    its volume are scoped to a `mngr_test-...` env that the session-end
    cleanup and the hourly CI safety net can both find.
    """
    environment_name = f"{modal_test_session_env_name}-{modal_test_session_user_id}"[
        :_MAX_MODAL_ENVIRONMENT_NAME_LENGTH
    ]
    app_name = _get_test_app_name()
    # The deployed function creates a volume named {app_name}-state
    volume_name = f"{app_name}-state"
    register_modal_test_app(app_name)
    register_modal_test_volume(volume_name)

    try:
        modal_interface = DirectModalInterface()
        _ensure_modal_environment(modal_interface, environment_name)
        url = _deploy_snapshot_function_with_propagation_retry(modal_interface, app_name, environment_name)
        # Warm up the function to avoid cold start timeouts in tests
        _warmup_function(url)
        yield (app_name, url, environment_name)
    finally:
        _stop_app_and_delete_volume(app_name, volume_name, environment_name)


@pytest.mark.acceptance
@pytest.mark.modal
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_success(
    deployed_snapshot_function: tuple[str, str, str],
) -> None:
    """Test successful snapshot and shutdown of a sandbox.

    Creates a sandbox, writes a host record, calls the endpoint, and verifies:
    1. The response indicates success
    2. The host record was updated with snapshot info
    3. The sandbox was terminated
    """
    app_name, function_url, environment_name = deployed_snapshot_function
    host_id = f"host-test-{get_short_random_string()}"

    # Create a test sandbox
    sandbox, sandbox_id = _create_test_sandbox(app_name, environment_name)

    try:
        # Write initial host record to volume
        _write_host_record_to_volume(app_name, host_id, environment_name)

        # Call the snapshot_and_shutdown endpoint
        response = httpx.post(
            function_url,
            json={
                "sandbox_id": sandbox_id,
                "host_id": host_id,
            },
            timeout=120,
        )

        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

        result = response.json()
        assert result["success"] is True, f"Expected success=True: {result}"
        assert "snapshot_id" in result
        # snapshot_id is now the Modal image ID (starts with "im-")
        assert result["snapshot_id"].startswith("im-")

        # Verify the host record was updated
        host_record = _read_host_record_from_volume(app_name, host_id, environment_name)
        assert host_record is not None, "Host record not found after snapshot"
        certified_data = host_record["certified_host_data"]
        assert len(certified_data["snapshots"]) == 1
        # The id IS the Modal image ID now
        assert certified_data["snapshots"][0]["id"] == result["snapshot_id"]
        # Verify stop_reason was set (defaults to PAUSED for idle shutdown)
        assert certified_data["stop_reason"] == HostState.PAUSED.value

        # Verify the sandbox was terminated by polling for termination
        def sandbox_terminated() -> bool:
            refreshed_sandbox = modal.Sandbox.from_id(sandbox_id)
            poll_result = refreshed_sandbox.poll()
            return poll_result is not None

        wait_for(sandbox_terminated, timeout=10.0, poll_interval=0.5, error_message="Sandbox should be terminated")

    finally:
        # Clean up sandbox if still running
        try:
            sandbox.terminate()
        except modal.exception.Error:
            pass


@pytest.mark.acceptance
@pytest.mark.modal
@pytest.mark.timeout(180)
@pytest.mark.flaky
def test_snapshot_and_shutdown_missing_sandbox_id(
    deployed_snapshot_function: tuple[str, str, str],
) -> None:
    """Test that missing sandbox_id returns 400 error."""
    _, function_url, _ = deployed_snapshot_function

    response = httpx.post(
        function_url,
        json={"host_id": "some-host-id"},
        timeout=60,
    )

    assert response.status_code == 400
    assert "sandbox_id" in response.text.lower()


@pytest.mark.acceptance
@pytest.mark.modal
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_missing_host_id(
    deployed_snapshot_function: tuple[str, str, str],
) -> None:
    """Test that missing host_id returns 400 error."""
    _, function_url, _ = deployed_snapshot_function

    response = httpx.post(
        function_url,
        json={"sandbox_id": "some-sandbox-id"},
        timeout=60,
    )

    assert response.status_code == 400
    assert "host_id" in response.text.lower()


@pytest.mark.acceptance
@pytest.mark.modal
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_nonexistent_sandbox(
    deployed_snapshot_function: tuple[str, str, str],
) -> None:
    """Test that a nonexistent sandbox returns 404 error."""
    app_name, function_url, environment_name = deployed_snapshot_function
    host_id = f"host-test-{get_short_random_string()}"

    # Write a host record so we can verify the sandbox lookup fails
    _write_host_record_to_volume(app_name, host_id, environment_name)

    response = httpx.post(
        function_url,
        json={
            "sandbox_id": "sb-nonexistent-id-12345",
            "host_id": host_id,
        },
        timeout=60,
    )

    assert response.status_code == 404
    assert "sandbox" in response.text.lower() or "not found" in response.text.lower()


@pytest.mark.acceptance
@pytest.mark.modal
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_nonexistent_host_record(
    deployed_snapshot_function: tuple[str, str, str],
) -> None:
    """Test that a nonexistent host record returns 404 error."""
    app_name, function_url, environment_name = deployed_snapshot_function
    host_id = f"host-nonexistent-{get_short_random_string()}"

    # Create a real sandbox but don't create a host record
    sandbox, sandbox_id = _create_test_sandbox(app_name, environment_name)

    try:
        response = httpx.post(
            function_url,
            json={
                "sandbox_id": sandbox_id,
                "host_id": host_id,
            },
            timeout=60,
        )

        assert response.status_code == 404
        assert "host" in response.text.lower() or "not found" in response.text.lower()

    finally:
        try:
            sandbox.terminate()
        except modal.exception.Error:
            pass
