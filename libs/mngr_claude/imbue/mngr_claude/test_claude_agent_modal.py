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

from imbue.mngr.utils.polling import poll_for_value
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
            # Plumb the test sandbox's ANTHROPIC_API_KEY into the agent env file
            # so claude inside the Modal agent can authenticate. The agent runs
            # in a separate Modal sandbox from this subprocess, so process-env
            # inheritance does not reach it.
            "--pass-env",
            "ANTHROPIC_API_KEY",
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


def _create_local_agent(
    agent_name: str,
    source_dir: Path,
    env: ModalSubprocessTestEnv,
    prompt: str = "just say 'hello'",
) -> subprocess.CompletedProcess[str]:
    """Create a Claude agent on the local (test sandbox) host.

    ``--yes`` flips ``mngr_ctx.is_auto_approve`` so the Claude trust dialog
    check is skipped for the fresh test work_dir (it would otherwise fail in
    a non-interactive environment because the work_dir isn't in the user's
    Claude trust list).
    """
    return subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            agent_name,
            "claude",
            "--yes",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(source_dir),
            "--pass-env",
            "ANTHROPIC_API_KEY",
            "--",
            "--dangerously-skip-permissions",
            "-p",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=env.env,
    )


def _clone_agent_to_modal(
    source_agent_name: str,
    target_agent_name: str,
    env: ModalSubprocessTestEnv,
) -> subprocess.CompletedProcess[str]:
    """Clone an existing agent to a fresh Modal host (different host id from source)."""
    return subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "clone",
            source_agent_name,
            f"{target_agent_name}@.modal",
            "--yes",
            "--no-connect",
            "--no-ensure-clean",
            "--pass-env",
            "ANTHROPIC_API_KEY",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=env.env,
    )


def _exec_on_modal_agent(
    agent_name: str,
    command: str,
    env: ModalSubprocessTestEnv,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command on a Modal-hosted agent's host."""
    return subprocess.run(
        ["uv", "run", "mngr", "exec", agent_name, command],
        capture_output=True,
        text=True,
        timeout=120,
        env=env.env,
    )


def _send_message_to_agent(
    agent_name: str,
    message: str,
    env: ModalSubprocessTestEnv,
) -> subprocess.CompletedProcess[str]:
    """Send ``message`` to a live claude agent via the mngr message bus."""
    return subprocess.run(
        ["uv", "run", "mngr", "message", agent_name, "--message", message],
        capture_output=True,
        text=True,
        timeout=120,
        env=env.env,
    )


def _wait_for_text_in_agent_pane(
    agent_name: str,
    expected: str,
    env: ModalSubprocessTestEnv,
    timeout: float,
) -> str:
    """Poll ``mngr capture --full`` until ``expected`` appears in the pane.

    Uses ``--full`` to read the entire scrollback so a fast-scrolling claude
    response isn't missed if it has already left the visible window by the
    time we look. Same pattern as ``test_adopt_session.py``'s
    ``_wait_for_text_in_pane``, just routed through ``mngr capture`` since
    the agent's tmux session lives on the Modal sandbox.
    """
    last_capture: list[str] = [""]

    def _capture_if_match() -> str | None:
        result = subprocess.run(
            ["uv", "run", "mngr", "capture", agent_name, "--full"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env.env,
        )
        last_capture[0] = result.stdout
        return result.stdout if expected in result.stdout else None

    capture, _, _ = poll_for_value(_capture_if_match, timeout=timeout, poll_interval=3.0)
    if capture is None:
        raise AssertionError(
            f"Did not see {expected!r} in mngr capture of agent {agent_name!r} within {timeout}s.\n"
            f"Last capture (tail):\n{last_capture[0][-3000:]}"
        )
    return capture


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
@pytest.mark.rsync
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
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_destroy_modal_agent_preserves_sessions_locally(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that destroying a Modal agent preserves session files to the local host_dir.

    This verifies the preserve_sessions_on_destroy feature end-to-end:
    1. Create a Claude agent on Modal with a prompt (session data is generated during create)
    2. Destroy the agent (triggers session file preservation)
    3. Verify that session files were pulled to the local preserved_sessions dir
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
@pytest.mark.rsync
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


def _read_preserved_session_text(host_dir: Path, agent_name: str) -> str:
    """Return the concatenated text of all preserved session JSONLs for an agent."""
    preserved_sessions_dir = host_dir / "plugin" / "mngr_claude" / "preserved_sessions"
    agent_dirs = [d for d in preserved_sessions_dir.iterdir() if d.is_dir() and d.name.startswith(agent_name)]
    assert len(agent_dirs) == 1, (
        f"Expected exactly one preserved dir for {agent_name}, "
        f"found: {[d.name for d in preserved_sessions_dir.iterdir()]}"
    )
    session_files = list((agent_dirs[0] / "projects").rglob("*.jsonl"))
    assert len(session_files) >= 1, f"No session .jsonl files under {agent_dirs[0] / 'projects'}"
    return "\n".join(f.read_text() for f in session_files)


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(900)
def test_clone_local_claude_agent_to_modal_resumes_session(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """End-to-end: cloning a local claude agent to Modal transfers the source's
    session data AND the cloned claude actually resumes the source's
    conversation (the model sees and acts on the source's history).

    Strategy (mirrors ``test_adopt_session.py``): plant a unique secret in
    the source's prompt, clone to Modal, and ask the cloned agent to
    recall the secret via ``mngr message`` + tmux pane capture. The recall
    response only contains the secret if claude on the clone actually
    resumed the source's session and saw the prior context.

    Three regressions this guards against, each of which would cause the
    cloned claude to start a fresh session and fail to recall the secret:

    1. Cross-host rsync (``_transfer_source_plugin_data``): plugin/ rsync
       must use the source agent's host as the rsync source, otherwise
       it runs on the destination looking for a path that doesn't exist
       there and aborts with ``rsync: change_dir failed``.

    2. Project-dir rekeying with the canonical work_dir
       (``_adopt_cloned_session``): the rsynced JSONL is filed under the
       source's encoded work_dir; claude searches under the
       ``readlink -f``-resolved encoding of the destination's work_dir
       (e.g. ``/mngr/projects/agent-X`` resolves to
       ``/__modal/volumes/<vol-id>/projects/agent-X`` on Modal). Without
       renaming the project subdir to match the resolved path, the
       rsynced JSONL is invisible to claude.

    3. Adopted session id (``_adopt_cloned_session``): the destination's
       ``claude_session_id`` (read by the startup command's
       ``MAIN_CLAUDE_SESSION_ID``) must be the source's actual session id
       (the JSONL filename's stem on disk) so the startup
       ``claude --resume "$MAIN_CLAUDE_SESSION_ID"`` targets the source's
       session. The source's own ``claude_session_id`` file holds the
       agent UUID written by the SessionStart hook default, which when
       claude ``-p`` ran did not match the JSONL claude actually wrote.
    """
    source_name = f"test-clone-src-{get_short_random_string()}"
    target_name = f"test-clone-dst-{get_short_random_string()}"
    _setup_claude_gitignore(temp_source_dir)

    # Two halves of a random secret -- the model can't accidentally guess
    # the combined form, and concatenation only happens if it actually saw
    # both halves in the resumed conversation.
    secret_left = f"hocus{get_short_random_string()}"
    secret_right = f"pocus{get_short_random_string()}"
    combined = secret_left + secret_right
    source_prompt = (
        f"Memorize these two separate tokens for later: '{secret_left}' and '{secret_right}'. "
        f"Just acknowledge with 'ok'."
    )
    recall_prompt = (
        "Combine the two tokens I asked you to memorize earlier into a single string with no "
        "space and no punctuation between them. Reply with just the combined string and nothing else."
    )

    create_result = _create_local_agent(source_name, temp_source_dir, modal_subprocess_env, prompt=source_prompt)
    assert create_result.returncode == 0, (
        f"Local create failed (rc={create_result.returncode}):\n"
        f"stdout: {create_result.stdout}\nstderr: {create_result.stderr}"
    )

    clone_result = _clone_agent_to_modal(source_name, target_name, modal_subprocess_env)
    assert clone_result.returncode == 0, (
        f"Clone to modal failed (rc={clone_result.returncode}):\n"
        f"stdout: {clone_result.stdout}\nstderr: {clone_result.stderr}"
    )

    # Drive the cloned agent: send the recall prompt and wait for the
    # combined secret to appear in the Modal sandbox's tmux pane (captured
    # via ``mngr capture``). The combined form does not appear in the
    # source's portion of the JSONL, so finding it in the cloned agent's
    # pane proves both (a) the cloned claude actually resumed the source's
    # session and (b) the model produced new content informed by it.
    msg_result = _send_message_to_agent(target_name, recall_prompt, modal_subprocess_env)
    assert msg_result.returncode == 0, (
        f"Sending recall message to clone failed (rc={msg_result.returncode}):\n"
        f"stdout: {msg_result.stdout}\nstderr: {msg_result.stderr}"
    )
    _wait_for_text_in_agent_pane(target_name, combined, modal_subprocess_env, timeout=240.0)

    # Sanity: the destroy path also preserves session content back to local.
    destroy_target_result = _destroy_modal_agent(target_name, modal_subprocess_env)
    assert destroy_target_result.returncode == 0, (
        f"Destroy of target failed (rc={destroy_target_result.returncode}):\n"
        f"stdout: {destroy_target_result.stdout}\nstderr: {destroy_target_result.stderr}"
    )
    _assert_sessions_preserved(modal_subprocess_env.host_dir, target_name)
    preserved_text = _read_preserved_session_text(modal_subprocess_env.host_dir, target_name)
    assert combined in preserved_text, (
        f"Combined secret '{combined}' missing from preserved JSONL after destroy "
        f"(claude's response wasn't appended to the resumed JSONL?). "
        f"First 2000 chars:\n{preserved_text[:2000]}"
    )
