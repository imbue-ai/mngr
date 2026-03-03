"""Integration and release tests for the snapshot CLI command."""

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pluggy
import pytest
from click.testing import CliRunner

from imbue.imbue_common.logging import log_span
from imbue.mng.cli.snapshot import snapshot
from imbue.mng.conftest import ModalSubprocessTestEnv
from imbue.mng.utils.testing import create_test_agent_via_cli
from imbue.mng.utils.testing import get_short_random_string
from imbue.mng.utils.testing import tmux_session_cleanup

# =============================================================================
# Tests with real local agents
# =============================================================================


@pytest.mark.tmux
def test_snapshot_create_local_agent_rejects_unsupported_provider(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot create fails for a local agent (unsupported provider)."""
    agent_name = f"test-snap-create-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            snapshot,
            ["create", agent_name],
            obj=plugin_manager,
            catch_exceptions=True,
        )

        assert result.exit_code != 0
        assert "does not support snapshots" in result.output


@pytest.mark.tmux
def test_snapshot_create_dry_run_jsonl_resolves_local_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --dry-run with --format jsonl outputs structured data on stdout."""
    agent_name = f"test-snap-dryrun-jsonl-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            snapshot,
            ["create", agent_name, "--dry-run", "--format", "jsonl"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "dry_run" in result.output
        assert agent_name in result.output


# =============================================================================
# Tests without agents (lightweight)
# =============================================================================


def test_snapshot_create_all_no_running_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot create --all succeeds when no agents are running."""
    result = cli_runner.invoke(
        snapshot,
        ["create", "--all"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_snapshot_list_all_no_running_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot list --all succeeds when no agents are running."""
    result = cli_runner.invoke(
        snapshot,
        ["list", "--all"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_snapshot_create_nonexistent_agent_errors(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot create for a nonexistent agent raises an error."""
    result = cli_runner.invoke(
        snapshot,
        ["create", "nonexistent-agent-99999"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


@pytest.mark.tmux
def test_snapshot_create_on_error_continue_reports_failure(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --on-error continue reports the error and exits 1 (doesn't crash)."""
    agent_name = f"test-snap-onerror-cont-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            snapshot,
            ["create", agent_name, "--on-error", "continue"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "does not support snapshots" in result.output or "Failed to create" in result.output


@pytest.mark.tmux
def test_snapshot_create_on_error_abort_reports_failure(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --on-error abort also fails (with abort message)."""
    agent_name = f"test-snap-onerror-abort-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            snapshot,
            ["create", agent_name, "--on-error", "abort"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "Aborted" in result.output or "does not support" in result.output


def test_snapshot_create_mixed_identifier_classified_as_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that a positional arg not matching any agent is treated as a host identifier.

    The identifier is classified as a host (no agent match), and since the local
    provider only accepts "localhost" as a host name, it fails with "not found".
    """
    result = cli_runner.invoke(
        snapshot,
        ["create", "not-an-agent-or-host-99999"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Agent or host not found" in result.output


def test_snapshot_list_nonexistent_agent_errors(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot list for a nonexistent agent raises an error."""
    result = cli_runner.invoke(
        snapshot,
        ["list", "nonexistent-agent-99999"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_snapshot_destroy_nonexistent_agent_errors(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot destroy for a nonexistent agent raises an error."""
    result = cli_runner.invoke(
        snapshot,
        ["destroy", "nonexistent-agent-99999", "--all-snapshots", "--force"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


# =============================================================================
# Release tests (require Modal credentials and network access)
# =============================================================================


def _extract_json(output: str) -> dict[str, Any]:
    """Extract the final JSON object from command output.

    When running CLI commands via subprocess, logger output may precede the
    JSON blob on stdout. This finds the last line that looks like JSON.
    """
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"No JSON found in output:\n{output}")


def _create_modal_agent(
    agent_name: str,
    source_dir: Path,
    env: dict[str, str],
) -> None:
    """Create a Modal agent via the CLI subprocess."""
    with log_span("Creating Modal agent for snapshot test", agent_name=agent_name):
        result = subprocess.run(
            [
                "uv",
                "run",
                "mng",
                "create",
                agent_name,
                "--agent-cmd",
                "sleep 3600",
                "--in",
                "modal",
                "--no-connect",
                "--await-ready",
                "--no-ensure-clean",
                "--source",
                str(source_dir),
                "--no-copy-work-dir",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        assert result.returncode == 0, f"Create agent failed: {result.stderr}\n{result.stdout}"


def _destroy_modal_agent(
    agent_name: str,
    env: dict[str, str],
) -> None:
    """Destroy a Modal agent via the CLI subprocess."""
    subprocess.run(
        ["uv", "run", "mng", "destroy", agent_name, "--force"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


@pytest.fixture
def temp_source_dir(tmp_path: Path) -> Path:
    """Create a temporary source directory for Modal tests."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "test.txt").write_text("test content")
    return source_dir


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(400)
def test_snapshot_create_then_list_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test snapshot functionality on a Modal agent

    Creates a real Modal agent, takes a snapshot, lists it to verify it exists
    """
    agent_name = f"test-snap-lifecycle-{get_short_random_string()}"
    env = modal_subprocess_env.env

    _create_modal_agent(agent_name, temp_source_dir, env)
    try:
        # Create a snapshot
        with log_span("Creating snapshot for Modal agent", agent_name=agent_name):
            create_result = subprocess.run(
                ["uv", "run", "mng", "snapshot", "create", agent_name, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            assert create_result.returncode == 0, (
                f"snapshot create failed: {create_result.stderr}\n{create_result.stdout}"
            )
            create_data = _extract_json(create_result.stdout)
            assert create_data["count"] == 1
            snapshot_id = create_data["snapshots_created"][0]["snapshot_id"]

        # List snapshots and verify the new one appears
        with log_span("Listing snapshots for Modal agent", agent_name=agent_name):
            list_result = subprocess.run(
                ["uv", "run", "mng", "snapshot", "list", agent_name, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            assert list_result.returncode == 0, f"snapshot list failed: {list_result.stderr}\n{list_result.stdout}"
            list_data = _extract_json(list_result.stdout)
            assert list_data["count"] >= 1
            listed_ids = [s["id"] for s in list_data["snapshots"]]
            assert snapshot_id in listed_ids

    finally:
        with log_span("Destroying Modal agent after snapshot test", agent_name=agent_name):
            _destroy_modal_agent(agent_name, env)


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.skip(
    "Just not worth the extra testing time right now (above and beyond what we're already getting via the above)"
)
@pytest.mark.timeout(300)
def test_snapshot_destroy_then_list_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test snapshot deletion on a Modal agent.

    Destroys all original snapshots and verifies they are gone.
    """
    agent_name = f"test-snap-lifecycle-{get_short_random_string()}"
    env = modal_subprocess_env.env

    _create_modal_agent(agent_name, temp_source_dir, env)
    try:
        # Destroy all
        with log_span("Destroying snapshots for Modal agent", agent_name=agent_name):
            destroy_result = subprocess.run(
                [
                    "uv",
                    "run",
                    "mng",
                    "snapshot",
                    "destroy",
                    agent_name,
                    "--all-snapshots",
                    "--force",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            assert destroy_result.returncode == 0
            destroy_data = _extract_json(destroy_result.stdout)
            assert destroy_data["count"] >= 2

        # Verify none remain
        with log_span("Verifying no snapshots remain for Modal agent", agent_name=agent_name):
            list_after = subprocess.run(
                ["uv", "run", "mng", "snapshot", "list", agent_name, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            assert list_after.returncode == 0
            assert _extract_json(list_after.stdout)["count"] == 0

    finally:
        with log_span("Destroying Modal agent after snapshot test", agent_name=agent_name):
            _destroy_modal_agent(agent_name, env)
