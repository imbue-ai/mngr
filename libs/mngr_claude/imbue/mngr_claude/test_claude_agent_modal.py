"""Release tests for Claude agent lifecycle on Modal.

These tests require Modal credentials, network access, and Claude credentials
to run. They are marked with @pytest.mark.release and only run when pushing
to main. To run them locally:

    PYTEST_MAX_DURATION_SECONDS=600 uv run pytest --no-cov --cov-fail-under=0 -n 0 -m release \\
        libs/mngr_claude/imbue/mngr_claude/test_claude_agent_modal.py
"""

import subprocess
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import ModalSubprocessTestEnv
from imbue.mngr.utils.testing import get_short_random_string


@pytest.fixture
def temp_source_dir(tmp_path: Path) -> Path:
    """Create a temporary source directory for tests."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    # Create a simple file so the directory isn't empty
    (source_dir / "test.txt").write_text("test content")
    return source_dir


def _setup_claude_gitignore(source_dir: Path) -> None:
    """Create .claude/ dir with settings and .gitignore to ignore local settings."""
    claude_settings_dir = source_dir / ".claude"
    claude_settings_dir.mkdir(exist_ok=True)
    (claude_settings_dir / "settings.local.json").write_text("{}")
    (source_dir / ".gitignore").write_text(".claude/settings.local.json\n")


def _create_modal_agent(
    agent_name: str,
    source_dir: Path,
    env: ModalSubprocessTestEnv,
) -> subprocess.CompletedProcess[str]:
    """Create a Claude agent on Modal and return the completed process."""
    return subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            f"{agent_name}@.modal",
            "claude",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(source_dir),
            "--",
            "--dangerously-skip-permissions",
            "-p",
            "just say 'hello'",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=env.env,
    )


def _destroy_modal_agent(agent_name: str, env: ModalSubprocessTestEnv) -> subprocess.CompletedProcess[str]:
    """Force-destroy a Modal agent and return the completed process."""
    return subprocess.run(
        ["uv", "run", "mngr", "destroy", agent_name, "--force"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env.env,
    )


def _stop_modal_agent(agent_name: str, env: ModalSubprocessTestEnv) -> subprocess.CompletedProcess[str]:
    """Stop a Modal agent and return the completed process."""
    return subprocess.run(
        ["uv", "run", "mngr", "stop", agent_name],
        capture_output=True,
        text=True,
        timeout=120,
        env=env.env,
    )


def _assert_sessions_preserved(host_dir: Path, agent_name: str) -> None:
    """Assert that session files were preserved for the given agent under host_dir.

    Checks that the preserved_sessions directory exists, contains exactly one
    directory for the agent, and that directory has at least one non-empty
    session JSONL file.
    """
    preserved_sessions_dir = host_dir / "plugin" / "mngr_claude" / "preserved_sessions"
    assert preserved_sessions_dir.exists(), f"Expected preserved_sessions dir at {preserved_sessions_dir}"

    # Find the agent's preserved directory (named <agent-name>--<agent-id>)
    agent_dirs = [d for d in preserved_sessions_dir.iterdir() if d.is_dir() and d.name.startswith(agent_name)]
    assert len(agent_dirs) == 1, (
        f"Expected exactly one preserved dir for {agent_name}, "
        f"found: {[d.name for d in preserved_sessions_dir.iterdir()]}"
    )
    agent_preserved_dir = agent_dirs[0]

    # At minimum, session JSONL files should be preserved (the projects/ directory)
    preserved_projects = agent_preserved_dir / "projects"
    assert preserved_projects.exists(), (
        f"Expected preserved projects/ dir. Contents: {list(agent_preserved_dir.iterdir())}"
    )
    session_files = list(preserved_projects.rglob("*.jsonl"))
    assert len(session_files) >= 1, f"Expected at least one session .jsonl file in {preserved_projects}"
    # Each session file should have content (Claude responded to the prompt)
    for session_file in session_files:
        assert session_file.stat().st_size > 0, f"Session file {session_file} is empty"


@pytest.mark.release
@pytest.mark.timeout(600)
def test_claude_agent_provisioning_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test creating a claude agent on Modal.

    This is an end-to-end release test that verifies:
    1. Claude agent can be provisioned on Modal
    2. Claude credentials are transferred correctly (if available locally)
    3. Claude is installed on the remote host
    4. The agent is created and started successfully

    The test uses --dangerously-skip-permissions -p "just say 'hello'" to run
    a quick, non-interactive claude session. The actual output goes to tmux,
    so we only verify that the agent was created successfully.
    """
    agent_name = f"test-claude-modal-{get_short_random_string()}"
    _setup_claude_gitignore(temp_source_dir)

    result = _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env)

    # Check that the command succeeded
    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"

    # Verify that Claude was installed (this message appears in the provisioning output)
    # This confirms that the claude plugin provisioning hook ran correctly
    combined_output = result.stdout + result.stderr
    assert "Claude installed successfully" in combined_output or "Claude is already installed" in combined_output, (
        f"Expected Claude installation message in output.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.release
@pytest.mark.timeout(600)
def test_destroy_modal_agent_preserves_sessions_locally(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that destroying a Modal agent preserves session files to the local host_dir.

    This verifies the preserve_sessions_on_destroy feature end-to-end:
    1. Create a Claude agent on Modal with a prompt
    2. Wait for session data to be generated
    3. Destroy the agent
    4. Verify that session files were pulled to the local preserved_sessions dir
    """
    agent_name = f"test-claude-preserve-{get_short_random_string()}"
    _setup_claude_gitignore(temp_source_dir)

    # Create the agent
    create_result = _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env)
    assert create_result.returncode == 0, (
        f"Create failed with stderr: {create_result.stderr}\nstdout: {create_result.stdout}"
    )

    # Destroy the agent (this should preserve session files locally).
    # No need to wait for idle -- mngr create with -p already waits for the
    # message to be sent, and Claude creates the session file at session start.
    destroy_result = _destroy_modal_agent(agent_name, modal_subprocess_env)
    assert destroy_result.returncode == 0, (
        f"Destroy failed with stderr: {destroy_result.stderr}\nstdout: {destroy_result.stdout}"
    )

    _assert_sessions_preserved(modal_subprocess_env.host_dir, agent_name)


@pytest.mark.release
@pytest.mark.timeout(600)
def test_destroy_stopped_modal_agent_preserves_sessions_from_volume(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that destroying a stopped (offline) Modal agent preserves sessions via the volume.

    When a Modal host goes offline (via stop), agent.on_destroy() cannot run
    because the sandbox is gone. Instead, the on_before_host_destroy hook reads
    session files directly from the host volume before it is deleted.

    Flow:
    1. Create a Claude agent on Modal with a prompt
    2. Stop the agent (sandbox terminates, data flushed to volume, host offline)
    3. Destroy the agent with --force (offline path, triggers on_before_host_destroy)
    4. Verify session files were preserved locally
    """
    agent_name = f"test-claude-vol-preserve-{get_short_random_string()}"
    _setup_claude_gitignore(temp_source_dir)

    # Create the agent
    create_result = _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env)
    assert create_result.returncode == 0, (
        f"Create failed with stderr: {create_result.stderr}\nstdout: {create_result.stdout}"
    )

    # Stop the agent (makes the host go offline, data flushed to volume)
    stop_result = _stop_modal_agent(agent_name, modal_subprocess_env)
    assert stop_result.returncode == 0, f"Stop failed with stderr: {stop_result.stderr}\nstdout: {stop_result.stdout}"

    # Destroy the agent (offline path -- on_before_host_destroy reads from volume)
    destroy_result = _destroy_modal_agent(agent_name, modal_subprocess_env)
    assert destroy_result.returncode == 0, (
        f"Destroy failed with stderr: {destroy_result.stderr}\nstdout: {destroy_result.stdout}"
    )

    _assert_sessions_preserved(modal_subprocess_env.host_dir, agent_name)
