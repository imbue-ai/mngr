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

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.conftest import register_modal_test_volume
from imbue.mng.primitives import HostState
from imbue.mng.providers.modal.constants import MODAL_TEST_APP_PREFIX
from imbue.mng.providers.modal.routes.deployment import deploy_function
from imbue.mng.utils.polling import wait_for
from imbue.mng.utils.testing import get_short_random_string

# =============================================================================
# Acceptance tests (require Modal network access)
# =============================================================================


class DeploymentError(RuntimeError):
    """Raised when deploying the Modal function fails."""


class URLParseError(RuntimeError):
    """Raised when the function URL cannot be parsed from deploy output."""


def _get_test_app_name() -> str:
    """Generate a unique test app name with the mng-test prefix."""
    return f"{MODAL_TEST_APP_PREFIX}snapshot-{get_short_random_string()}"


def _stop_app(app_name: str) -> None:
    """Stop and clean up a Modal app."""
    subprocess.run(
        ["uv", "run", "modal", "app", "stop", app_name],
        input=b"y\n",
        capture_output=True,
        timeout=60,
    )


def _delete_volume(volume_name: str) -> None:
    """Delete a Modal volume."""
    subprocess.run(
        ["uv", "run", "modal", "volume", "delete", volume_name, "--yes"],
        capture_output=True,
        timeout=60,
    )


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


def _create_test_sandbox(app_name: str) -> tuple[modal.Sandbox, str]:
    """Create a test sandbox within the given app.

    Creates a simple sandbox that sleeps, suitable for testing snapshot functionality.
    """
    app = modal.App.lookup(app_name, create_if_missing=True)
    sandbox = modal.Sandbox.create(
        app=app,
        image=modal.Image.debian_slim(),
        timeout=300,
    )
    sandbox.exec("sleep", "3600")
    return sandbox, sandbox.object_id


def _write_host_record_to_volume(app_name: str, host_id: str) -> None:
    """Write a host record to the Modal volume for testing.

    Creates a minimal host record that the snapshot function can update.
    The structure matches HostRecord model with nested certified_host_data.
    """
    volume_name = f"{app_name}-state"
    register_modal_test_volume(volume_name)
    volume = modal.Volume.from_name(volume_name, create_if_missing=True)

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


def _read_host_record_from_volume(app_name: str, host_id: str) -> dict[str, Any] | None:
    """Read a host record from the Modal volume."""
    volume_name = f"{app_name}-state"
    register_modal_test_volume(volume_name)
    volume = modal.Volume.from_name(volume_name)

    try:
        content = b"".join(volume.read_file(f"/hosts/{host_id}.json"))
        return json.loads(content.decode("utf-8"))
    except modal.exception.NotFoundError:
        return None


@pytest.fixture(scope="module")
def deployed_snapshot_function() -> Generator[tuple[str, str], None, None]:
    """Deploy the snapshot function for testing and clean up after.

    Yields a tuple of (app_name, function_url).
    """
    app_name = _get_test_app_name()
    # The deployed function creates a volume named {app_name}-state
    volume_name = f"{app_name}-state"
    register_modal_test_volume(volume_name)

    try:
        with ConcurrencyGroup(name="test_deploy_snapshot") as cg:
            url = deploy_function("snapshot_and_shutdown", app_name, None, cg)
        # Warm up the function to avoid cold start timeouts in tests
        _warmup_function(url)
        yield (app_name, url)
    finally:
        _stop_app(app_name)
        _delete_volume(volume_name)


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_success(
    deployed_snapshot_function: tuple[str, str],
) -> None:
    """Test successful snapshot and shutdown of a sandbox.

    Creates a sandbox, writes a host record, calls the endpoint, and verifies:
    1. The response indicates success
    2. The host record was updated with snapshot info
    3. The sandbox was terminated
    """
    app_name, function_url = deployed_snapshot_function
    host_id = f"host-test-{get_short_random_string()}"

    # Create a test sandbox
    sandbox, sandbox_id = _create_test_sandbox(app_name)

    try:
        # Write initial host record to volume
        _write_host_record_to_volume(app_name, host_id)

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
        host_record = _read_host_record_from_volume(app_name, host_id)
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
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_missing_sandbox_id(
    deployed_snapshot_function: tuple[str, str],
) -> None:
    """Test that missing sandbox_id returns 400 error."""
    _, function_url = deployed_snapshot_function

    response = httpx.post(
        function_url,
        json={"host_id": "some-host-id"},
        timeout=60,
    )

    assert response.status_code == 400
    assert "sandbox_id" in response.text.lower()


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_missing_host_id(
    deployed_snapshot_function: tuple[str, str],
) -> None:
    """Test that missing host_id returns 400 error."""
    _, function_url = deployed_snapshot_function

    response = httpx.post(
        function_url,
        json={"sandbox_id": "some-sandbox-id"},
        timeout=60,
    )

    assert response.status_code == 400
    assert "host_id" in response.text.lower()


@pytest.mark.acceptance
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_nonexistent_sandbox(
    deployed_snapshot_function: tuple[str, str],
) -> None:
    """Test that a nonexistent sandbox returns 404 error."""
    app_name, function_url = deployed_snapshot_function
    host_id = f"host-test-{get_short_random_string()}"

    # Write a host record so we can verify the sandbox lookup fails
    _write_host_record_to_volume(app_name, host_id)

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
@pytest.mark.timeout(180)
def test_snapshot_and_shutdown_nonexistent_host_record(
    deployed_snapshot_function: tuple[str, str],
) -> None:
    """Test that a nonexistent host record returns 404 error."""
    app_name, function_url = deployed_snapshot_function
    host_id = f"host-nonexistent-{get_short_random_string()}"

    # Create a real sandbox but don't create a host record
    sandbox, sandbox_id = _create_test_sandbox(app_name)

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
