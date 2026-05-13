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
def test_clone_local_claude_agent_to_modal_rekeys_for_resume(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """End-to-end: cloning a local claude agent to Modal must (a) transfer the
    source's plugin/ session data and (b) rewrite it for the destination's
    work_dir encoding + session id so the cloned claude *attempts* to resume
    the source's session.

    Two distinct regressions this guards against:

    1. **Cross-host rsync** -- the original bug from #1598. The plugin/
       rsync ran on the destination Modal sandbox looking for the source
       plugin dir there and aborted with
       ``rsync: change_dir ".../plugin" failed: No such file or directory``.

    2. **Session rekeying** -- session JSONLs are filed under the source's
       encoded work_dir, and ``claude_session_id`` (read by the startup
       command's ``MAIN_CLAUDE_SESSION_ID``) lives outside ``plugin/``.
       ``_adopt_cloned_session`` (a) renames the project subdir to the
       destination's encoded work_dir so claude can find the file under
       its expected path, and (b) writes the source's JSONL stem to
       ``claude_session_id`` so the startup ``claude --resume`` targets
       it.

    Verifies via ``mngr exec`` against the live cloned sandbox that:

    * The project subdir was renamed (one ``-mngr-projects-...`` subdir
      and no ``-private-var-...`` / ``-tmp-...`` source-encoded leftover).
    * ``claude_session_id_history`` records the adopted source session id
      as the first ``startup`` entry -- proof that the agent's startup
      script ran with ``MAIN_CLAUDE_SESSION_ID`` resolved to the source's
      session id (which can only happen if ``_adopt_cloned_session``
      wrote it to ``claude_session_id`` before agent startup).

    NOTE: actually getting the cloned claude to *successfully* resume and
    use the source context (i.e. drive it via ``mngr message`` and see
    the model produce content informed by the source) is not yet working
    end-to-end. The startup ``claude --resume <source-sid>`` is attempted
    (visible in ``claude_session_id_history``) but exits nonzero, falling
    back through the startup command's ``||`` to ``claude --session-id
    <clone-agent-uuid>`` which starts a fresh session. The remaining
    issue is inside claude itself (possibly session-id-vs-internal-id
    mismatch, or claude's resume rejecting a JSONL it didn't author).
    Tracking that down is a separate follow-up.
    """
    source_name = f"test-clone-src-{get_short_random_string()}"
    target_name = f"test-clone-dst-{get_short_random_string()}"
    _setup_claude_gitignore(temp_source_dir)

    secret = f"hocus{get_short_random_string()}pocus"
    source_prompt = f"Memorize this secret token: '{secret}'. Just acknowledge with 'ok'."

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

    # Project subdir was renamed: exactly the destination's encoding
    # ("-mngr-projects-...") is present, source's local-fs encoding is not.
    list_result = _exec_on_modal_agent(
        target_name,
        "ls /mngr/agents/*/plugin/claude/anthropic/projects/",
        modal_subprocess_env,
    )
    assert list_result.returncode == 0, (
        f"Listing projects dir on clone failed:\nstdout: {list_result.stdout}\nstderr: {list_result.stderr}"
    )
    project_subdirs = [line.strip() for line in list_result.stdout.split("\n") if line.strip()]
    # Drop mngr exec's trailing status line.
    project_subdirs = [n for n in project_subdirs if not n.startswith("Command succeeded")]
    assert any(name.startswith("-mngr-projects-") for name in project_subdirs), (
        f"No clone-encoded project subdir on the clone (rekeying regressed?). Saw: {project_subdirs}"
    )
    assert not any(name.startswith("-private-var") or name.startswith("-tmp-") for name in project_subdirs), (
        f"Source-encoded project subdir still present on the clone (rekeying did not rename). Saw: {project_subdirs}"
    )

    # The adopted source session id should appear as the FIRST line of the
    # startup history -- written by claude's SessionStart hook when it ran
    # ``claude --resume "$MAIN_CLAUDE_SESSION_ID"`` with our adopted id.
    # Find the source's session id from the JSONL filename on the clone.
    jsonl_result = _exec_on_modal_agent(
        target_name,
        "find /mngr/agents/*/plugin/claude/anthropic/projects -maxdepth 2 -name '*.jsonl' -type f",
        modal_subprocess_env,
    )
    jsonl_files = [
        line.strip()
        for line in jsonl_result.stdout.split("\n")
        if line.strip() and not line.startswith("Command succeeded")
    ]
    assert jsonl_files, f"No JSONL found on clone (plugin transfer regressed?). stdout: {jsonl_result.stdout!r}"
    adopted_session_id = Path(jsonl_files[0]).stem

    history_result = _exec_on_modal_agent(
        target_name, "cat /mngr/agents/*/claude_session_id_history", modal_subprocess_env
    )
    history_lines = [
        line for line in history_result.stdout.split("\n") if line and not line.startswith("Command succeeded")
    ]
    assert history_lines and history_lines[0].startswith(f"{adopted_session_id} "), (
        f"Expected first startup history entry to be the adopted source session id "
        f"{adopted_session_id!r} (proof that the startup command saw "
        f"MAIN_CLAUDE_SESSION_ID = source's id, i.e. _adopt_cloned_session "
        f"wrote claude_session_id correctly before agent startup). "
        f"history_lines={history_lines!r}"
    )

    # Sanity: the source's content also survived the destroy round trip.
    destroy_target_result = _destroy_modal_agent(target_name, modal_subprocess_env)
    assert destroy_target_result.returncode == 0, (
        f"Destroy of target failed (rc={destroy_target_result.returncode}):\n"
        f"stdout: {destroy_target_result.stdout}\nstderr: {destroy_target_result.stderr}"
    )
    _assert_sessions_preserved(modal_subprocess_env.host_dir, target_name)
    preserved_text = _read_preserved_session_text(modal_subprocess_env.host_dir, target_name)
    assert secret in preserved_text, (
        f"Source's secret token '{secret}' missing from preserved JSONL "
        f"(plugin transfer regressed?). First 2000 chars:\n{preserved_text[:2000]}"
    )
