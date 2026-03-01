import json
import time
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.create import create
from imbue.mng.cli.provision import provision
from imbue.mng.cli.stop import stop
from imbue.mng.conftest import ModalSubprocessTestEnv
from imbue.mng.utils.testing import create_test_agent_via_cli
from imbue.mng.utils.testing import get_short_random_string
from imbue.mng.utils.testing import run_mng_subprocess
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists


@pytest.mark.tmux
def test_provision_existing_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that provisioning an existing agent succeeds."""
    agent_name = f"test-provision-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            provision,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"


@pytest.mark.tmux
def test_provision_with_user_command(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that provisioning with --user-command executes the command."""
    agent_name = f"test-prov-cmd-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    marker_file = tmp_path / "provision_marker.txt"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            provision,
            [
                agent_name,
                "--user-command",
                f"echo 'provisioned' > {marker_file}",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"
        assert marker_file.exists(), "User command should have created the marker file"
        assert marker_file.read_text().strip() == "provisioned"


@pytest.mark.tmux
def test_provision_with_env_var(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    per_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that provisioning with --env sets environment variables."""
    agent_name = f"test-prov-env-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            provision,
            [
                agent_name,
                "--env",
                "MY_NEW_VAR=hello_world",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"

        # Verify the env var was written to the agent's env file
        agents_dir = per_host_dir / "agents"
        assert agents_dir.exists()
        agent_dirs = list(agents_dir.iterdir())
        assert len(agent_dirs) == 1
        env_file = agent_dirs[0] / "env"
        assert env_file.exists()
        env_content = env_file.read_text()
        assert "MY_NEW_VAR=hello_world" in env_content


@pytest.mark.tmux
def test_provision_preserves_existing_env_vars(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    per_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that provisioning preserves existing environment variables."""
    agent_name = f"test-prov-env-preserve-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create agent with initial env vars
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 849127",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
                "--env",
                "INITIAL_VAR=original_value",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Create failed with: {result.output}"

        # Re-provision with a new env var
        result = cli_runner.invoke(
            provision,
            [
                agent_name,
                "--env",
                "ADDED_VAR=new_value",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Provision failed with: {result.output}"

        # Verify both env vars are present
        agents_dir = per_host_dir / "agents"
        agent_dirs = list(agents_dir.iterdir())
        assert len(agent_dirs) == 1
        env_file = agent_dirs[0] / "env"
        env_content = env_file.read_text()
        assert "INITIAL_VAR=original_value" in env_content
        assert "ADDED_VAR=new_value" in env_content


@pytest.mark.tmux
def test_provision_with_upload_file(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that provisioning with --upload-file transfers the file."""
    agent_name = f"test-prov-upload-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    # Create a local file to upload
    local_file = tmp_path / "upload_source.txt"
    local_file.write_text("uploaded content")
    remote_path = tmp_path / "upload_destination.txt"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            provision,
            [
                agent_name,
                "--upload-file",
                f"{local_file}:{remote_path}",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"
        assert remote_path.exists(), "Upload should have created the destination file"
        assert remote_path.read_text() == "uploaded content"


def test_provision_agent_not_found(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that provisioning a non-existent agent raises an error."""
    result = cli_runner.invoke(
        provision,
        ["nonexistent-agent-93847562"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0


@pytest.mark.tmux
def test_provision_with_agent_option(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --agent option works as an alternative to positional argument."""
    agent_name = f"test-prov-opt-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            provision,
            [
                "--agent",
                agent_name,
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"


def test_provision_both_positional_and_option_raises_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that specifying both positional agent and --agent raises an error."""
    result = cli_runner.invoke(
        provision,
        [
            "my-agent",
            "--agent",
            "other-agent",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot specify both" in result.output


@pytest.mark.tmux
def test_provision_json_output(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --format json produces JSON output."""
    agent_name = f"test-prov-json-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        result = cli_runner.invoke(
            provision,
            [
                agent_name,
                "--format",
                "json",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"
        assert '"provisioned": true' in result.output


@pytest.mark.tmux
def test_provision_stopped_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that provisioning a stopped agent succeeds.

    This is a regression test: provision needs the host online but does not
    need the agent process running. Previously, provisioning a stopped agent
    failed because the agent lookup required the agent to be running.
    """
    agent_name = f"test-prov-stopped-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        # Stop the agent
        stop_result = cli_runner.invoke(
            stop,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert stop_result.exit_code == 0, f"Stop failed with: {stop_result.output}"

        # Provision the stopped agent -- should succeed
        result = cli_runner.invoke(
            provision,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision stopped agent failed with: {result.output}"


@pytest.mark.tmux
def test_provision_stopped_agent_with_user_command(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that provisioning a stopped agent executes user commands.

    Regression test: verifies that user commands run even when the agent
    process is stopped, since provisioning operates on the host, not
    the agent process.
    """
    agent_name = f"test-prov-stopped-cmd-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    marker_file = tmp_path / "stopped_provision_marker.txt"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        # Stop the agent
        stop_result = cli_runner.invoke(
            stop,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert stop_result.exit_code == 0, f"Stop failed with: {stop_result.output}"

        # Provision the stopped agent with a user command
        result = cli_runner.invoke(
            provision,
            [
                agent_name,
                "--user-command",
                f"echo 'provisioned-while-stopped' > {marker_file}",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision stopped agent failed with: {result.output}"
        assert marker_file.exists(), "User command should have created the marker file"
        assert marker_file.read_text().strip() == "provisioned-while-stopped"


@pytest.mark.tmux
def test_provision_running_agent_restarts_by_default(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that provisioning a running agent restarts it by default.

    The agent should be stopped before provisioning and restarted after,
    and should be running after provisioning completes.
    """
    agent_name = f"test-prov-restart-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        # Verify agent is running before provisioning
        assert tmux_session_exists(session_name), "Agent should be running before provision"

        # Provision with default settings (restart=True)
        result = cli_runner.invoke(
            provision,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"

        # Agent should still be running after provisioning (restarted)
        assert tmux_session_exists(session_name), "Agent should be running after provision with restart"


@pytest.mark.tmux
def test_provision_running_agent_no_restart_keeps_running(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --no-restart does not stop/restart a running agent."""
    agent_name = f"test-prov-norestart-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        # Verify agent is running before provisioning
        assert tmux_session_exists(session_name), "Agent should be running before provision"

        # Provision with --no-restart
        result = cli_runner.invoke(
            provision,
            [agent_name, "--no-restart"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"

        # Agent should still be running (was never stopped)
        assert tmux_session_exists(session_name), "Agent should still be running after provision with --no-restart"


@pytest.mark.tmux
def test_provision_stopped_agent_stays_stopped_with_restart(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that provisioning a stopped agent does not start it, even with restart enabled.

    When is_restart=True, only agents that were running before provisioning should
    be restarted. A stopped agent should remain stopped.
    """
    agent_name = f"test-prov-stopped-norestart-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name)

        # Stop the agent
        stop_result = cli_runner.invoke(
            stop,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert stop_result.exit_code == 0, f"Stop failed with: {stop_result.output}"

        # Verify agent is stopped
        assert not tmux_session_exists(session_name), "Agent should be stopped"

        # Provision with default settings (restart=True)
        result = cli_runner.invoke(
            provision,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Provision failed with: {result.output}"

        # Agent should still be stopped (it was not running when provision started)
        assert not tmux_session_exists(session_name), "Stopped agent should remain stopped after provision"


# =============================================================================
# Acceptance tests for provisioning stopped Modal agents
# =============================================================================


def _get_agent_info(agent_name: str, env: dict[str, str]) -> dict | None:
    """Get agent info from `mng list --format json`. Returns None if not found."""
    result = run_mng_subprocess("list", "--format", "json", env=env, timeout=60)
    assert result.returncode == 0, f"mng list failed: {result.stderr}\n{result.stdout}"
    data = json.loads(result.stdout)
    for agent in data.get("agents", []):
        if agent.get("name") == agent_name:
            return agent
    return None


@pytest.mark.acceptance
@pytest.mark.timeout(600)
def test_provision_stopped_modal_agent(
    tmp_path: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Provisioning a stopped Modal agent preserves identity and data.

    Regression test: previously, `mng provision` failed on stopped Modal
    agents because (1) agents on destroyed/offline hosts were not included
    in the search, and (2) the agent lookup required the agent process to
    be running.

    This test verifies that after create -> stop -> provision:
    - The agent ID is preserved
    - Existing env vars are preserved
    - User commands execute on the host
    """
    agent_name = f"test-modal-prov-stopped-{get_short_random_string()}"
    env_marker = f"PROV_TEST_MARKER={get_short_random_string()}"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "test.txt").write_text("test content")
    env = modal_subprocess_env.env

    # Create agent with an env var we can check for preservation
    result = run_mng_subprocess(
        "create",
        agent_name,
        "generic",
        "--in",
        "modal",
        "--no-connect",
        "--await-ready",
        "--no-ensure-clean",
        "--no-copy-work-dir",
        "--source",
        str(source_dir),
        "--agent-cmd",
        "sleep 999999",
        "--env",
        env_marker,
        env=env,
        timeout=300,
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}\n{result.stdout}"

    # Record agent ID before stop
    agent_info_before = _get_agent_info(agent_name, env)
    assert agent_info_before is not None, f"Agent {agent_name} not found after create"
    agent_id_before = agent_info_before["id"]

    # Stop the agent (destroys the Modal sandbox)
    result = run_mng_subprocess("stop", agent_name, env=env, timeout=120)
    assert result.returncode == 0, f"Stop failed: {result.stderr}\n{result.stdout}"

    # Provision the stopped agent with a new env var and a user command
    new_env_var = f"PROV_NEW_VAR={get_short_random_string()}"
    result = run_mng_subprocess(
        "provision",
        agent_name,
        "--env",
        new_env_var,
        "--user-command",
        "echo 'provision-ran' > /tmp/prov_marker.txt",
        env=env,
        timeout=300,
    )
    assert result.returncode == 0, f"Provision stopped agent failed: {result.stderr}\n{result.stdout}"

    # Verify agent identity is preserved
    agent_info_after = _get_agent_info(agent_name, env)
    assert agent_info_after is not None, f"Agent {agent_name} not found after provision"
    assert agent_info_after["id"] == agent_id_before, (
        f"Agent ID changed: {agent_id_before} -> {agent_info_after['id']}"
    )
