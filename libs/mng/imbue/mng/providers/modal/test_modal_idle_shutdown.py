"""Acceptance tests for Modal idle shutdown and snapshot creation.

These tests verify that the idle shutdown flow works correctly:
1. Host is created with activity_watcher.sh running
2. Initial snapshot is created during agent creation
3. When the host becomes idle, activity_watcher calls shutdown.sh
4. shutdown.sh calls the snapshot_and_shutdown Modal endpoint
5. The endpoint creates a snapshot and terminates the sandbox
6. The offline host record contains both snapshots

These tests require Modal credentials and network access. They are marked
with @pytest.mark.acceptance and are skipped by default. To run them:

    pytest -m acceptance --timeout=300 -k idle_shutdown
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from imbue.mng.primitives import HostState
from imbue.mng.testing.testing import ModalSubprocessTestEnv
from imbue.mng.testing.testing import get_short_random_string
from imbue.mng.utils.polling import wait_for


class MngListError(Exception):
    """Error raised when mng list fails."""

    pass


def _run_mng_list_json(env: dict[str, str], provider: str) -> dict:
    """Run mng list with JSON output and return the parsed result.

    Raises MngListError if the command fails.
    """
    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "list",
            "--provider",
            provider,
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.returncode != 0:
        raise MngListError(f"mng list failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def _get_host_snapshots(
    env: dict[str, str],
    provider: str,
    host_name: str,
    *,
    tolerate_errors: bool = False,
) -> list[dict]:
    """Get the snapshots for a host by name.

    If tolerate_errors is True, returns empty list on listing errors (useful for polling).
    """
    try:
        list_result = _run_mng_list_json(env, provider)
    except MngListError:
        if tolerate_errors:
            return []
        raise
    for agent in list_result.get("agents", []):
        host = agent.get("host", {})
        if host.get("name") == host_name:
            return host.get("snapshots", [])
    return []


def _get_host_state(
    env: dict[str, str],
    provider: str,
    host_name: str,
    *,
    tolerate_errors: bool = False,
) -> str | None:
    """Get the state of a host by name.

    If tolerate_errors is True, returns None on listing errors (useful for polling during transitions).
    """
    try:
        list_result = _run_mng_list_json(env, provider)
    except MngListError:
        if tolerate_errors:
            # During host transitions (e.g., sandbox terminating), SSH/SFTP errors
            # can occur. Return None to indicate we couldn't determine the state.
            return None
        raise
    for agent in list_result.get("agents", []):
        host = agent.get("host", {})
        if host.get("name") == host_name:
            return host.get("state")
    return None


@pytest.mark.skip(
    "This runs locally, fails remotely, I think because the make_tar_of_repo.sh script fails to create a current.tar.gz"
)
@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_idle_shutdown_creates_both_initial_and_idle_snapshots(
    tmp_path: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that idle shutdown creates both initial and idle snapshots.

    This test verifies the full idle shutdown flow:
    1. Creates a sandbox with a short idle timeout (15 seconds)
    2. Creates an agent (which triggers initial snapshot creation)
    3. Waits for the host to become idle and shut down
    4. Verifies the offline host has both snapshots:
       - Initial snapshot (created during agent creation)
       - Idle snapshot (created during shutdown)
    """
    # Use a unique agent name for this test
    agent_name = f"test-idle-snap-{get_short_random_string()}"

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    # Create a simple file so the directory isn't empty
    (source_dir / "test.txt").write_text("test content for idle shutdown test")

    tar_dir = tmp_path / "tar_output"
    tar_dir.mkdir()
    temp_dir_with_tar = str(tar_dir)
    commit_hash = os.environ.get("GITHUB_SHA", "") or Path(".mng/image_commit_hash").read_text().strip()

    # go make the tar
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"./scripts/make_tar_of_repo.sh {commit_hash} {temp_dir_with_tar}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
        env=modal_subprocess_env.env,
    )

    # Create an agent with:
    # - Very short idle timeout (15 seconds) so it shuts down quickly
    # - Short sandbox timeout (120 seconds) with buffer time for clean shutdown
    # - Echo command that exits immediately so the host becomes idle
    # - idle-mode=boot so only BOOT activity is checked (not PROCESS which
    #   keeps getting updated while the tmux bash shell is running)
    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "generic",
            "--in",
            "modal",
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(source_dir),
            # Set idle timeout to 15 seconds
            "--idle-timeout",
            "15",
            # Use boot idle mode so only BOOT activity is checked
            # (the default IO mode includes PROCESS which keeps getting
            # updated while the tmux bash shell is alive after echo exits)
            "--idle-mode",
            "boot",
            # Set sandbox timeout to 120 seconds via build args
            "-b",
            "--timeout=120",
            # use our dockerfile since it should end up being cached and faster
            "-b",
            "--file=libs/mng/imbue/mng/resources/Dockerfile",
            "-b",
            "context-dir=.mng/dev/build/",
            "--",
            "echo hi && sleep 300",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"Agent creation failed: {result.stderr}\n{result.stdout}"

    # Get the host name from the agent (should match agent name for Modal)
    list_result = _run_mng_list_json(modal_subprocess_env.env, "modal")
    host_name = None
    for agent in list_result.get("agents", []):
        if agent.get("name") == agent_name:
            host_name = agent.get("host", {}).get("name")
            break

    assert host_name is not None, f"Could not find host for agent {agent_name}"

    # Verify initial snapshot was created (should happen during agent creation)
    initial_snapshots = _get_host_snapshots(modal_subprocess_env.env, "modal", host_name)
    assert len(initial_snapshots) >= 1, (
        f"Expected at least 1 snapshot (initial) after agent creation, "
        f"but found {len(initial_snapshots)}: {initial_snapshots}"
    )
    initial_snapshot_names = [s.get("name") for s in initial_snapshots]
    assert "initial" in initial_snapshot_names, (
        f"Expected 'initial' snapshot after agent creation, but found snapshots: {initial_snapshot_names}"
    )

    # Wait for the host to become idle and shut down
    # The echo command exits immediately, so the host should become idle
    # after the 15-second idle timeout, then the activity_watcher will
    # call shutdown.sh which calls snapshot_and_shutdown
    #
    # Total wait time budget:
    # - ~15 seconds for idle timeout
    # - ~60 seconds for activity_watcher check interval
    # - ~30 seconds for snapshot and shutdown
    # = ~105 seconds total, use 150 for safety margin
    def host_is_offline() -> bool:
        # Use tolerate_errors=True during polling because SSH/SFTP errors
        # can occur during the transition period when the sandbox is terminating.
        # We keep polling until we can successfully query the host state.
        state = _get_host_state(modal_subprocess_env.env, "modal", host_name, tolerate_errors=True)
        # Host should be in a non-running state (STOPPED, PAUSED, DESTROYED, etc.)
        # If state is None, we couldn't query it (transient error), so keep polling.
        return state is not None and state not in (
            HostState.RUNNING.value,
            HostState.STARTING.value,
            HostState.BUILDING.value,
        )

    wait_for(
        host_is_offline,
        timeout=150.0,
        poll_interval=10.0,
        error_message=f"Host {host_name} did not shut down within 150 seconds",
    )

    # Verify the offline host has both snapshots
    final_snapshots = _get_host_snapshots(modal_subprocess_env.env, "modal", host_name)
    assert len(final_snapshots) >= 2, (
        f"Expected at least 2 snapshots (initial + idle), but found {len(final_snapshots)}: {final_snapshots}"
    )

    # Verify we have both the initial and idle snapshot
    final_snapshot_names = [s.get("name") for s in final_snapshots]
    assert "initial" in final_snapshot_names, (
        f"Expected 'initial' snapshot in final snapshots, but found: {final_snapshot_names}"
    )

    # The idle snapshot should have a name like "snapshot-XXXXXXXX"
    idle_snapshot_names = [n for n in final_snapshot_names if n != "initial"]
    assert len(idle_snapshot_names) >= 1, (
        f"Expected at least one non-initial snapshot (idle snapshot), but found only: {final_snapshot_names}"
    )

    # Verify the host state is 'paused' (idle shutdown sets stop_reason=PAUSED)
    final_state = _get_host_state(modal_subprocess_env.env, "modal", host_name)
    assert final_state == HostState.PAUSED.value, (
        f"Expected host state to be '{HostState.PAUSED.value}' after idle shutdown, but got: {final_state}"
    )
