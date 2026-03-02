"""Release tests for the claude agent provisioning on Modal.

These tests require Modal credentials, network access, and Claude credentials
to run. They are marked with @pytest.mark.release and only run when pushing
to main. To run them locally:

    PYTEST_MAX_DURATION=600 uv run pytest --no-cov --cov-fail-under=0 -n 0 -m release \\
        libs/mng/imbue/mng/agents/default_plugins/test_claude_agent_modal.py::test_claude_agent_provisioning_on_modal
"""

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mng.conftest import ModalSubprocessTestEnv


@pytest.fixture
def temp_source_dir(tmp_path: Path) -> Path:
    """Create a temporary source directory for tests."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    # Create a simple file so the directory isn't empty
    (source_dir / "test.txt").write_text("test content")
    return source_dir


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
    # Use a unique agent name with globally unique id to avoid collisions
    unique_id = uuid4().hex[:12]
    agent_name = f"test-claude-modal-{unique_id}"

    # make a .gitignore file to ignore the claude local settings
    claude_settings_dir = temp_source_dir / ".claude"
    claude_settings_dir.mkdir()
    (claude_settings_dir / "settings.local.json").write_text("{}")
    (temp_source_dir / ".gitignore").write_text(".claude/settings.local.json\n")

    # Run mng create with claude agent on modal
    # Using --no-connect and --await-ready to run synchronously without attaching
    # Using --no-ensure-clean since temp dir won't be a git repo
    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "claude",
            "--in",
            "modal",
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "--",
            "--dangerously-skip-permissions",
            "-p",
            "just say 'hello'",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=modal_subprocess_env.env,
    )

    # Check that the command succeeded
    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"

    # Verify that Claude was installed (this message appears in the provisioning output)
    # This confirms that the claude plugin provisioning hook ran correctly
    combined_output = result.stdout + result.stderr
    assert "Claude installed successfully" in combined_output or "Claude is already installed" in combined_output, (
        f"Expected Claude installation message in output.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
