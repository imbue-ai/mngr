"""Release tests for Claude agent lifecycle on Modal.

These tests require Modal credentials, network access, and Claude credentials
to run. They are marked with @pytest.mark.release and only run when pushing
to main. To run them locally:

    PYTEST_MAX_DURATION_SECONDS=600 uv run pytest --no-cov --cov-fail-under=0 -n 0 -m release \\
        libs/mngr_claude/imbue/mngr_claude/test_claude_agent_modal.py
"""

import json
import subprocess
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import poll_until
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


def _wait_for_agent_idle(agent_name: str, env: ModalSubprocessTestEnv, timeout_seconds: float = 120.0) -> None:
    """Poll until the agent reaches WAITING or STOPPED state (indicating it finished processing)."""

    def _is_agent_idle() -> bool:
        result = subprocess.run(
            ["uv", "run", "mngr", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env.env,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.strip().splitlines():
            try:
                agent_data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if agent_data.get("name") == agent_name and agent_data.get("state") in ("WAITING", "STOPPED"):
                return True
        return False

    is_idle = poll_until(_is_agent_idle, timeout=timeout_seconds, poll_interval=5.0)
    if not is_idle:
        pytest.fail(f"Agent {agent_name} did not reach idle state within {timeout_seconds}s")


def _destroy_modal_agent(agent_name: str, env: ModalSubprocessTestEnv) -> subprocess.CompletedProcess[str]:
    """Force-destroy a Modal agent and return the completed process."""
    return subprocess.run(
        ["uv", "run", "mngr", "destroy", agent_name, "--force"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env.env,
    )


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

    # Wait for Claude to process the prompt and generate session data
    _wait_for_agent_idle(agent_name, modal_subprocess_env)

    # Destroy the agent (this should preserve session files locally)
    destroy_result = _destroy_modal_agent(agent_name, modal_subprocess_env)
    assert destroy_result.returncode == 0 or "Destroyed agent" in destroy_result.stdout, (
        f"Destroy failed with stderr: {destroy_result.stderr}\nstdout: {destroy_result.stdout}"
    )

    # Verify that session files were preserved under the local host_dir
    preserved_sessions_dir = modal_subprocess_env.host_dir / "plugin" / "mngr_claude" / "preserved_sessions"
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
