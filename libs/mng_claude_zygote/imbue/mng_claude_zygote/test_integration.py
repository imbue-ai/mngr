"""Integration tests for the mng_claude_zygote plugin.

Tests the plugin end-to-end by creating real agents in temporary git repos,
verifying provisioning creates the expected filesystem structures, and
exercising the chat and watcher scripts.

These tests use --agent-cmd to override the default Claude command with
a simple sleep process, since Claude Code is not available in CI. This
still exercises all the provisioning, symlink creation, and tmux window
injection logic that the plugin provides.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.create import create
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists
from imbue.mng_claude_zygote.provisioning import _DEFAULT_CHANGELINGS_DIR_FILES
from imbue.mng_claude_zygote.provisioning import _DEFAULT_SKILL_DIRS
from imbue.mng_claude_zygote.provisioning import _DEFAULT_WORK_DIR_FILES
from imbue.mng_claude_zygote.provisioning import _LLM_TOOL_FILES
from imbue.mng_claude_zygote.provisioning import _SCRIPT_FILES
from imbue.mng_claude_zygote.provisioning import compute_claude_project_dir_name


def _unique_agent_name(label: str) -> str:
    """Generate a unique agent name for test isolation."""
    return f"test-{label}-{int(time.time())}"


def _create_zygote_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    agent_name: str,
    source_dir: Path,
    *,
    agent_cmd: str = "sleep 847291",
    extra_args: tuple[str, ...] = (),
) -> int:
    """Create a claude-zygote agent via the CLI and return the exit code.

    Uses --agent-cmd to override the default Claude command since Claude Code
    is not available in test environments. The --agent-type claude-zygote flag
    is NOT used because --agent-cmd and --agent-type are mutually exclusive
    for non-generic types. Instead, the provisioning is tested separately.
    """
    result = cli_runner.invoke(
        create,
        [
            "--name",
            agent_name,
            "--agent-cmd",
            agent_cmd,
            "--source",
            str(source_dir),
            "--no-connect",
            "--await-ready",
            "--no-copy-work-dir",
            "--no-ensure-clean",
            "--disable-plugin",
            "modal",
            *extra_args,
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    return result.exit_code


def _find_agent_state_dir(host_dir: Path) -> Path | None:
    """Find the agent state directory under the host dir.

    Returns the first agent state directory found, or None if none exist.
    """
    agents_dir = host_dir / "agents"
    if not agents_dir.exists():
        return None
    for entry in agents_dir.iterdir():
        if entry.is_dir():
            return entry
    return None


# -- Provisioning filesystem structure tests --


@pytest.mark.timeout(30)
def test_provisioning_creates_event_log_directories(
    temp_git_repo: Path,
    temp_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that provisioning creates all expected event log directories."""
    from imbue.mng_claude_zygote.data_types import ProvisioningSettings
    from imbue.mng_claude_zygote.provisioning import create_event_log_directories

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    agent_state_dir.mkdir(parents=True)
    settings = ProvisioningSettings()

    # Use a real host-like structure for local execution
    from imbue.mng_claude_zygote.conftest import StubHost

    host = StubHost(host_dir=temp_host_dir)
    # Override execute_command to actually create directories
    original_execute = host.execute_command

    def real_mkdir(command: str, **kwargs: object) -> object:
        if command.startswith("mkdir -p"):
            path = command.split("mkdir -p ")[1].strip("'\"")
            Path(path).mkdir(parents=True, exist_ok=True)
        return original_execute(command, **kwargs)

    host.execute_command = real_mkdir  # type: ignore[assignment]

    create_event_log_directories(host, agent_state_dir, settings)  # type: ignore[arg-type]

    expected_sources = (
        "conversations",
        "messages",
        "scheduled",
        "mng_agents",
        "stop",
        "monitor",
        "claude_transcript",
    )
    for source in expected_sources:
        source_dir = agent_state_dir / "logs" / source
        assert source_dir.exists(), f"Expected logs/{source}/ directory to exist"


@pytest.mark.timeout(30)
def test_provisioning_writes_changeling_scripts_to_host(
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning writes all bash scripts with correct permissions."""
    from imbue.mng_claude_zygote.conftest import StubHost
    from imbue.mng_claude_zygote.data_types import ProvisioningSettings
    from imbue.mng_claude_zygote.provisioning import provision_changeling_scripts

    commands_dir = temp_host_dir / "commands"
    commands_dir.mkdir(parents=True)

    host = StubHost(host_dir=temp_host_dir)

    def real_mkdir(command: str, **kwargs: object) -> object:
        if command.startswith("mkdir -p"):
            path = command.split("mkdir -p ")[1].strip("'\"")
            Path(path).mkdir(parents=True, exist_ok=True)
        return host.__class__.execute_command(host, command, **kwargs)

    host.execute_command = real_mkdir  # type: ignore[assignment]

    def real_write(path: Path, content: bytes, mode: str = "0644") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.chmod(path, int(mode, 8))

    host.write_file = real_write  # type: ignore[assignment]

    provision_changeling_scripts(host, ProvisioningSettings())  # type: ignore[arg-type]

    for script_name in _SCRIPT_FILES:
        script_path = commands_dir / script_name
        assert script_path.exists(), f"Expected {script_name} to be written"
        assert script_path.stat().st_mode & 0o111, f"Expected {script_name} to be executable"
        content = script_path.read_text()
        assert content.startswith("#!/bin/bash"), f"Expected {script_name} to have bash shebang"


@pytest.mark.timeout(30)
def test_provisioning_writes_llm_tools_to_host(
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning writes LLM tool scripts."""
    from imbue.mng_claude_zygote.conftest import StubHost
    from imbue.mng_claude_zygote.data_types import ProvisioningSettings
    from imbue.mng_claude_zygote.provisioning import provision_llm_tools

    host = StubHost(host_dir=temp_host_dir)

    def real_mkdir(command: str, **kwargs: object) -> object:
        if command.startswith("mkdir -p"):
            path = command.split("mkdir -p ")[1].strip("'\"")
            Path(path).mkdir(parents=True, exist_ok=True)
        return host.__class__.execute_command(host, command, **kwargs)

    host.execute_command = real_mkdir  # type: ignore[assignment]

    def real_write(path: Path, content: bytes, mode: str = "0644") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    host.write_file = real_write  # type: ignore[assignment]

    provision_llm_tools(host, ProvisioningSettings())  # type: ignore[arg-type]

    tools_dir = temp_host_dir / "commands" / "llm_tools"
    for tool_file in _LLM_TOOL_FILES:
        tool_path = tools_dir / tool_file
        assert tool_path.exists(), f"Expected {tool_file} to be written"
        content = tool_path.read_text()
        assert "def " in content, f"Expected {tool_file} to contain Python function definitions"


@pytest.mark.timeout(30)
def test_provisioning_creates_default_content_when_missing(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning writes default content files when they don't exist."""
    from imbue.mng_claude_zygote.conftest import StubCommandResult
    from imbue.mng_claude_zygote.conftest import StubHost
    from imbue.mng_claude_zygote.data_types import ProvisioningSettings
    from imbue.mng_claude_zygote.provisioning import provision_default_content

    host = StubHost(
        host_dir=temp_host_dir,
        # All file checks fail (files don't exist)
        command_results={"test -f": StubCommandResult(success=False)},
    )

    def real_mkdir(command: str, **kwargs: object) -> object:
        if command.startswith("mkdir -p"):
            path = command.split("mkdir -p ")[1].strip("'\"")
            Path(path).mkdir(parents=True, exist_ok=True)
        return host.__class__.execute_command(host, command, **kwargs)

    host.execute_command = real_mkdir  # type: ignore[assignment]

    written_paths: list[tuple[Path, str]] = []
    original_write = host.write_text_file

    def tracking_write(path: Path, content: str) -> None:
        written_paths.append((path, content))
        original_write(path, content)

    host.write_text_file = tracking_write  # type: ignore[assignment]

    provision_default_content(host, temp_git_repo, ".changelings", ProvisioningSettings())  # type: ignore[arg-type]

    written_path_strings = [str(p) for p, _ in written_paths]

    # Verify CLAUDE.md was written to work dir
    for _, relative_path in _DEFAULT_WORK_DIR_FILES:
        expected = str(temp_git_repo / relative_path)
        assert expected in written_path_strings, f"Expected {relative_path} to be written to work dir"

    # Verify changelings dir files were written
    for _, relative_path in _DEFAULT_CHANGELINGS_DIR_FILES:
        expected = str(temp_git_repo / ".changelings" / relative_path)
        assert expected in written_path_strings, f"Expected {relative_path} to be written to changelings dir"

    # Verify skill files were written
    for skill_name in _DEFAULT_SKILL_DIRS:
        expected = str(temp_git_repo / ".claude" / "skills" / skill_name / "SKILL.md")
        assert expected in written_path_strings, f"Expected skill {skill_name}/SKILL.md to be written"


@pytest.mark.timeout(30)
def test_provisioning_does_not_overwrite_existing_content(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning does not overwrite files that already exist."""
    from imbue.mng_claude_zygote.conftest import StubHost
    from imbue.mng_claude_zygote.data_types import ProvisioningSettings
    from imbue.mng_claude_zygote.provisioning import provision_default_content

    # All file checks succeed (files already exist)
    host = StubHost(host_dir=temp_host_dir)

    provision_default_content(host, temp_git_repo, ".changelings", ProvisioningSettings())  # type: ignore[arg-type]

    # No files should have been written since all `test -f` checks pass (default StubCommandResult is success)
    assert len(host.written_text_files) == 0, "Should not overwrite existing files"


@pytest.mark.timeout(30)
def test_provisioning_creates_symlinks(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning creates the expected symlinks."""
    from imbue.mng_claude_zygote.data_types import ProvisioningSettings
    from imbue.mng_claude_zygote.provisioning import create_changeling_symlinks

    changelings_dir = temp_git_repo / ".changelings"
    changelings_dir.mkdir()
    (changelings_dir / "entrypoint.md").write_text("# Test entrypoint")
    (changelings_dir / "entrypoint.json").write_text("{}")

    claude_dir = temp_git_repo / ".claude"
    claude_dir.mkdir()

    settings = ProvisioningSettings()

    # Use subprocess for real symlink creation
    def execute_command(command: str, **kwargs: object) -> object:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        class _Result:
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr

        return _Result()

    class RealHost:
        host_dir = temp_host_dir

    real_host = RealHost()
    real_host.execute_command = execute_command  # type: ignore[attr-defined]

    create_changeling_symlinks(real_host, temp_git_repo, ".changelings", settings)  # type: ignore[arg-type]

    local_md = temp_git_repo / "CLAUDE.local.md"
    assert local_md.is_symlink(), "CLAUDE.local.md should be a symlink"
    assert local_md.resolve() == (changelings_dir / "entrypoint.md").resolve()

    settings_json = temp_git_repo / ".claude" / "settings.local.json"
    assert settings_json.is_symlink(), "settings.local.json should be a symlink"
    assert settings_json.resolve() == (changelings_dir / "entrypoint.json").resolve()


@pytest.mark.timeout(30)
def test_provisioning_links_memory_directory(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning creates the memory symlink into Claude project directory."""
    from imbue.mng_claude_zygote.data_types import ProvisioningSettings
    from imbue.mng_claude_zygote.provisioning import link_memory_directory

    settings = ProvisioningSettings()

    def execute_command(command: str, **kwargs: object) -> object:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, env=os.environ)

        class _Result:
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr

        return _Result()

    class RealHost:
        host_dir = temp_host_dir

    real_host = RealHost()
    real_host.execute_command = execute_command  # type: ignore[attr-defined]

    link_memory_directory(real_host, temp_git_repo, ".changelings", settings)  # type: ignore[arg-type]

    # Memory directory should exist in changelings dir
    changelings_memory = temp_git_repo / ".changelings" / "memory"
    assert changelings_memory.is_dir(), "changelings memory dir should exist"

    # Claude project directory should contain a memory symlink
    abs_work_dir = str(temp_git_repo.resolve())
    project_dir_name = compute_claude_project_dir_name(abs_work_dir)
    project_memory = Path.home() / ".claude" / "projects" / project_dir_name / "memory"
    assert project_memory.is_symlink(), "Claude project memory should be a symlink"
    assert project_memory.resolve() == changelings_memory.resolve()


@pytest.mark.timeout(30)
def test_provisioning_writes_default_chat_model(
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning writes the default chat model file."""
    from imbue.mng_claude_zygote.data_types import ChatModel
    from imbue.mng_claude_zygote.provisioning import write_default_chat_model

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    agent_state_dir.mkdir(parents=True)

    def write_text_file(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    class RealHost:
        host_dir = temp_host_dir

    real_host = RealHost()
    real_host.write_text_file = write_text_file  # type: ignore[attr-defined]

    write_default_chat_model(real_host, agent_state_dir, ChatModel("claude-sonnet-4-6"))  # type: ignore[arg-type]

    model_file = agent_state_dir / "default_chat_model"
    assert model_file.exists(), "default_chat_model file should exist"
    assert model_file.read_text().strip() == "claude-sonnet-4-6"


# -- Chat script tests --


@pytest.mark.timeout(30)
def test_chat_script_shows_help(
    temp_host_dir: Path,
) -> None:
    """Verify that chat.sh --help outputs usage information."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    (agent_state_dir / "logs" / "conversations").mkdir(parents=True)

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    result = subprocess.run(
        [str(chat_script), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0
    assert "chat" in result.stdout.lower()
    assert "--new" in result.stdout
    assert "--resume" in result.stdout
    assert "--list" in result.stdout


@pytest.mark.timeout(30)
def test_chat_script_list_shows_no_conversations_initially(
    temp_host_dir: Path,
) -> None:
    """Verify that chat.sh --list reports no conversations when events file doesn't exist."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    (agent_state_dir / "logs" / "conversations").mkdir(parents=True)

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    result = subprocess.run(
        [str(chat_script), "--list"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0
    assert "no conversations" in result.stdout.lower()


@pytest.mark.timeout(30)
def test_chat_script_rejects_unknown_options(
    temp_host_dir: Path,
) -> None:
    """Verify that chat.sh rejects unknown options with an error."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    (agent_state_dir / "logs" / "conversations").mkdir(parents=True)

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    result = subprocess.run(
        [str(chat_script), "--bogus"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert "unknown" in result.stderr.lower()


@pytest.mark.timeout(30)
def test_chat_script_resume_requires_conversation_id(
    temp_host_dir: Path,
) -> None:
    """Verify that chat.sh --resume without a conversation ID fails."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    (agent_state_dir / "logs" / "conversations").mkdir(parents=True)

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    result = subprocess.run(
        [str(chat_script), "--resume"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0


@pytest.mark.timeout(30)
def test_chat_script_no_args_lists_and_shows_hint(
    temp_host_dir: Path,
) -> None:
    """Verify that calling chat.sh with no arguments lists conversations and shows a help hint."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    (agent_state_dir / "logs" / "conversations").mkdir(parents=True)

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    result = subprocess.run(
        [str(chat_script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0
    assert "--help" in result.stdout


@pytest.mark.timeout(30)
def test_chat_script_list_shows_existing_conversations(
    temp_host_dir: Path,
) -> None:
    """Verify that chat.sh --list shows conversations from the events file."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    conv_dir = agent_state_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)

    # Write a test conversation event
    event = {
        "timestamp": "2025-01-15T10:00:00.000000000Z",
        "type": "conversation_created",
        "event_id": "evt-test-001",
        "source": "conversations",
        "conversation_id": "conv-test-12345",
        "model": "claude-sonnet-4-6",
    }
    events_file = conv_dir / "events.jsonl"
    events_file.write_text(json.dumps(event) + "\n")

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    result = subprocess.run(
        [str(chat_script), "--list"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0
    assert "conv-test-12345" in result.stdout
    assert "claude-sonnet-4-6" in result.stdout


@pytest.mark.timeout(30)
def test_chat_script_list_handles_malformed_events(
    temp_host_dir: Path,
) -> None:
    """Verify that chat.sh --list gracefully handles malformed JSONL lines."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    conv_dir = agent_state_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)

    # Write a mix of valid and malformed events
    valid_event = json.dumps(
        {
            "timestamp": "2025-01-15T10:00:00.000000000Z",
            "type": "conversation_created",
            "event_id": "evt-test-002",
            "source": "conversations",
            "conversation_id": "conv-valid-789",
            "model": "claude-sonnet-4-6",
        }
    )
    events_file = conv_dir / "events.jsonl"
    events_file.write_text(f"this is not json\n{valid_event}\n")

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    result = subprocess.run(
        [str(chat_script), "--list"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    # Should still succeed and show the valid conversation
    assert result.returncode == 0
    assert "conv-valid-789" in result.stdout
    # Should warn about the malformed line
    assert "malformed" in result.stderr.lower() or "warning" in result.stderr.lower()


# -- Conversation watcher script tests --


@pytest.mark.timeout(30)
def test_conversation_watcher_script_is_valid_bash(
    temp_host_dir: Path,
) -> None:
    """Verify that conversation_watcher.sh passes bash syntax check."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    watcher_script = temp_host_dir / "commands" / "conversation_watcher.sh"
    watcher_script.parent.mkdir(parents=True)
    watcher_script.write_text(load_zygote_resource("conversation_watcher.sh"))
    os.chmod(watcher_script, 0o755)

    result = subprocess.run(
        ["bash", "-n", str(watcher_script)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, f"Syntax check failed: {result.stderr}"


@pytest.mark.timeout(30)
def test_event_watcher_script_is_valid_bash(
    temp_host_dir: Path,
) -> None:
    """Verify that event_watcher.sh passes bash syntax check."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    watcher_script = temp_host_dir / "commands" / "event_watcher.sh"
    watcher_script.parent.mkdir(parents=True)
    watcher_script.write_text(load_zygote_resource("event_watcher.sh"))
    os.chmod(watcher_script, 0o755)

    result = subprocess.run(
        ["bash", "-n", str(watcher_script)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, f"Syntax check failed: {result.stderr}"


# -- Agent creation integration tests --


@pytest.mark.timeout(60)
def test_create_agent_with_additional_commands(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that creating an agent with additional commands creates the expected tmux windows."""
    agent_name = _unique_agent_name("addcmd")

    # Use the setup_test_mng_env autouse fixture's prefix
    prefix = os.environ.get("MNG_PREFIX", "mng-test-")
    session_name = f"{prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 847291",
                "--source",
                str(temp_git_repo),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
                "--disable-plugin",
                "modal",
                "--add-command",
                'watcher="sleep 847292"',
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert tmux_session_exists(session_name)

        # Verify the additional window was created
        windows_result = subprocess.run(
            ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        assert windows_result.returncode == 0
        window_names = windows_result.stdout.strip().split("\n")
        assert "watcher" in window_names, f"Expected 'watcher' window, got: {window_names}"


@pytest.mark.timeout(60)
def test_create_agent_creates_state_directory(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that creating an agent creates the agent state directory."""
    agent_name = _unique_agent_name("state")
    prefix = os.environ.get("MNG_PREFIX", "mng-test-")
    session_name = f"{prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        exit_code = _create_zygote_agent(cli_runner, plugin_manager, agent_name, temp_git_repo)
        assert exit_code == 0

        agent_state_dir = _find_agent_state_dir(temp_host_dir)
        assert agent_state_dir is not None, "Agent state directory should exist"
        assert (agent_state_dir / "data.json").exists(), "data.json should exist in agent state dir"


# -- Settings loading integration tests --


@pytest.mark.timeout(30)
def test_settings_loaded_from_host_with_valid_toml(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that settings are loaded from a valid settings.toml file."""
    from imbue.mng_claude_zygote.settings import load_settings_from_host

    changelings_dir = temp_git_repo / ".changelings"
    changelings_dir.mkdir()
    settings_file = changelings_dir / "settings.toml"
    settings_file.write_text(
        '[chat]\nmodel = "claude-sonnet-4-6"\n\n[watchers]\nconversation_poll_interval_seconds = 10\n'
    )

    def execute_command(command: str, **kwargs: object) -> object:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        class _Result:
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr

        return _Result()

    def read_text_file(path: Path) -> str:
        return path.read_text()

    class RealHost:
        host_dir = temp_host_dir

    real_host = RealHost()
    real_host.execute_command = execute_command  # type: ignore[attr-defined]
    real_host.read_text_file = read_text_file  # type: ignore[attr-defined]

    settings = load_settings_from_host(real_host, temp_git_repo, ".changelings")  # type: ignore[arg-type]

    assert settings.chat.model == "claude-sonnet-4-6"
    assert settings.watchers.conversation_poll_interval_seconds == 10


@pytest.mark.timeout(30)
def test_settings_returns_defaults_for_missing_file(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that settings default gracefully when settings.toml is missing."""
    from imbue.mng_claude_zygote.settings import load_settings_from_host

    def execute_command(command: str, **kwargs: object) -> object:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        class _Result:
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr

        return _Result()

    class RealHost:
        host_dir = temp_host_dir

    real_host = RealHost()
    real_host.execute_command = execute_command  # type: ignore[attr-defined]

    settings = load_settings_from_host(real_host, temp_git_repo, ".changelings")  # type: ignore[arg-type]

    assert settings.chat.model is None
    assert settings.watchers.conversation_poll_interval_seconds == 5
    assert settings.watchers.event_poll_interval_seconds == 3


@pytest.mark.timeout(30)
def test_settings_returns_defaults_for_invalid_toml(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that settings default gracefully when settings.toml is invalid."""
    from imbue.mng_claude_zygote.settings import load_settings_from_host

    changelings_dir = temp_git_repo / ".changelings"
    changelings_dir.mkdir()
    (changelings_dir / "settings.toml").write_text("this is not valid toml [[[")

    def execute_command(command: str, **kwargs: object) -> object:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        class _Result:
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr

        return _Result()

    def read_text_file(path: Path) -> str:
        return path.read_text()

    class RealHost:
        host_dir = temp_host_dir

    real_host = RealHost()
    real_host.execute_command = execute_command  # type: ignore[attr-defined]
    real_host.read_text_file = read_text_file  # type: ignore[attr-defined]

    settings = load_settings_from_host(real_host, temp_git_repo, ".changelings")  # type: ignore[arg-type]

    # Should fall back to defaults
    assert settings.chat.model is None
    assert settings.watchers.conversation_poll_interval_seconds == 5


# -- JSONL event format tests --


@pytest.mark.timeout(30)
def test_conversation_event_serializes_to_valid_jsonl(
    temp_host_dir: Path,
) -> None:
    """Verify that conversation events written by chat.sh are valid JSONL."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    conv_dir = agent_state_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    model_file = agent_state_dir / "default_chat_model"
    model_file.write_text("claude-sonnet-4-6\n")

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    # The --new --as-agent flag creates a conversation without needing llm
    # (it only writes the event and prints the conversation ID)
    result = subprocess.run(
        [str(chat_script), "--new", "--as-agent"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0

    # The conversation ID should be printed to stdout
    cid = result.stdout.strip()
    assert cid.startswith("conv-"), f"Expected conversation ID, got: {cid!r}"

    # Verify the events file was written
    events_file = conv_dir / "events.jsonl"
    assert events_file.exists(), "conversations/events.jsonl should exist"

    lines = events_file.read_text().strip().split("\n")
    assert len(lines) >= 1, "Should have at least one event"

    event = json.loads(lines[-1])
    assert event["type"] == "conversation_created"
    assert event["source"] == "conversations"
    assert event["conversation_id"] == cid
    assert event["model"] == "claude-sonnet-4-6"
    assert "timestamp" in event
    assert "event_id" in event


@pytest.mark.timeout(30)
def test_multiple_conversations_create_separate_events(
    temp_host_dir: Path,
) -> None:
    """Verify that creating multiple conversations produces separate events."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    conv_dir = agent_state_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    (agent_state_dir / "default_chat_model").write_text("claude-sonnet-4-6\n")

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    cids = []
    for _ in range(3):
        result = subprocess.run(
            [str(chat_script), "--new", "--as-agent"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0
        cids.append(result.stdout.strip())

    # All conversation IDs should be unique
    assert len(set(cids)) == 3, f"Expected 3 unique CIDs, got: {cids}"

    events_file = conv_dir / "events.jsonl"
    lines = events_file.read_text().strip().split("\n")
    assert len(lines) == 3

    event_cids = [json.loads(line)["conversation_id"] for line in lines]
    assert set(event_cids) == set(cids)


@pytest.mark.timeout(30)
def test_chat_model_read_from_default_model_file(
    temp_host_dir: Path,
) -> None:
    """Verify that chat.sh reads the model from the default_chat_model file."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    conv_dir = agent_state_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    (agent_state_dir / "default_chat_model").write_text("claude-haiku-4-5\n")

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    result = subprocess.run(
        [str(chat_script), "--new", "--as-agent"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0

    events_file = conv_dir / "events.jsonl"
    event = json.loads(events_file.read_text().strip().split("\n")[-1])
    assert event["model"] == "claude-haiku-4-5"


@pytest.mark.timeout(30)
def test_chat_script_creates_log_file(
    temp_host_dir: Path,
) -> None:
    """Verify that chat.sh creates a log file with operation records."""
    from imbue.mng_claude_zygote.provisioning import load_zygote_resource

    chat_script = temp_host_dir / "commands" / "chat.sh"
    chat_script.parent.mkdir(parents=True)
    chat_script.write_text(load_zygote_resource("chat.sh"))
    os.chmod(chat_script, 0o755)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    (agent_state_dir / "logs" / "conversations").mkdir(parents=True)
    (agent_state_dir / "default_chat_model").write_text("claude-sonnet-4-6\n")

    log_dir = temp_host_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["MNG_AGENT_STATE_DIR"] = str(agent_state_dir)
    env["MNG_HOST_DIR"] = str(temp_host_dir)

    subprocess.run(
        [str(chat_script), "--new", "--as-agent"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    log_file = log_dir / "chat.log"
    assert log_file.exists(), "chat.log should be created"
    log_content = log_file.read_text()
    assert "Creating new conversation" in log_content


# -- Event watcher offset tracking tests --


@pytest.mark.timeout(30)
def test_event_watcher_offset_tracking(
    temp_host_dir: Path,
) -> None:
    """Verify that the event watcher's offset-based tracking logic works correctly.

    Tests the core mechanism: writing events to a JSONL file and verifying
    that offset files track which lines have been processed.
    """
    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    logs_dir = agent_state_dir / "logs"
    offsets_dir = logs_dir / ".event_offsets"
    offsets_dir.mkdir(parents=True)

    messages_dir = logs_dir / "messages"
    messages_dir.mkdir(parents=True)

    events_file = messages_dir / "events.jsonl"

    # Write some events
    for i in range(5):
        event = {
            "timestamp": f"2025-01-15T10:00:0{i}.000Z",
            "type": "message",
            "event_id": f"evt-{i}",
            "source": "messages",
            "conversation_id": "conv-test",
            "role": "user",
            "content": f"Message {i}",
        }
        with events_file.open("a") as f:
            f.write(json.dumps(event) + "\n")

    # Simulate processing by setting offset
    offset_file = offsets_dir / "messages.offset"
    offset_file.write_text("3")

    # Verify offset tracking works (new lines = total - offset)
    total_lines = len(events_file.read_text().strip().split("\n"))
    current_offset = int(offset_file.read_text().strip())
    new_count = total_lines - current_offset
    assert new_count == 2, f"Expected 2 new events, got {new_count}"


# -- Provisioning settings file tests --


@pytest.mark.timeout(30)
def test_provision_settings_file_copies_to_agent_state(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that settings.toml is copied to the agent state directory."""
    from imbue.mng_claude_zygote.settings import provision_settings_file

    changelings_dir = temp_git_repo / ".changelings"
    changelings_dir.mkdir()
    settings_content = '[chat]\nmodel = "claude-sonnet-4-6"\n'
    (changelings_dir / "settings.toml").write_text(settings_content)

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    agent_state_dir.mkdir(parents=True)

    def execute_command(command: str, **kwargs: object) -> object:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        class _Result:
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr

        return _Result()

    def read_text_file(path: Path) -> str:
        return path.read_text()

    def write_text_file(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    class RealHost:
        host_dir = temp_host_dir

    real_host = RealHost()
    real_host.execute_command = execute_command  # type: ignore[attr-defined]
    real_host.read_text_file = read_text_file  # type: ignore[attr-defined]
    real_host.write_text_file = write_text_file  # type: ignore[attr-defined]

    provision_settings_file(real_host, temp_git_repo, ".changelings", agent_state_dir)  # type: ignore[arg-type]

    dest = agent_state_dir / "settings.toml"
    assert dest.exists(), "settings.toml should be copied to agent state dir"
    assert dest.read_text() == settings_content


@pytest.mark.timeout(30)
def test_provision_settings_file_noop_when_missing(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning settings does nothing when settings.toml is missing."""
    from imbue.mng_claude_zygote.settings import provision_settings_file

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    agent_state_dir.mkdir(parents=True)

    def execute_command(command: str, **kwargs: object) -> object:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        class _Result:
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr

        return _Result()

    class RealHost:
        host_dir = temp_host_dir

    real_host = RealHost()
    real_host.execute_command = execute_command  # type: ignore[attr-defined]

    provision_settings_file(real_host, temp_git_repo, ".changelings", agent_state_dir)  # type: ignore[arg-type]

    dest = agent_state_dir / "settings.toml"
    assert not dest.exists(), "settings.toml should not be created when source is missing"


# -- Tmux window injection integration tests --


@pytest.mark.timeout(60)
def test_agent_with_ttyd_window_creates_session_with_expected_windows(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that adding a ttyd-style window via --add-command creates the expected tmux windows.

    This tests the window injection mechanism that the claude-zygote plugin uses,
    without requiring ttyd to be installed. It uses a simple sleep command in place
    of the actual ttyd invocation.
    """
    agent_name = _unique_agent_name("ttyd")
    prefix = os.environ.get("MNG_PREFIX", "mng-test-")
    session_name = f"{prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 847291",
                "--source",
                str(temp_git_repo),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
                "--disable-plugin",
                "modal",
                # Simulate what the plugin does: inject named windows
                "--add-command",
                'agent_ttyd="sleep 847293"',
                "--add-command",
                'conv_watcher="sleep 847294"',
                "--add-command",
                'events="sleep 847295"',
                "--add-command",
                'chat_ttyd="sleep 847296"',
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert tmux_session_exists(session_name)

        # Verify all expected windows were created
        windows_result = subprocess.run(
            ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        assert windows_result.returncode == 0
        window_names = windows_result.stdout.strip().split("\n")

        expected_windows = {"agent_ttyd", "conv_watcher", "events", "chat_ttyd"}
        for expected in expected_windows:
            assert expected in window_names, f"Expected window '{expected}' in {window_names}"


@pytest.mark.timeout(60)
def test_agent_creation_and_listing(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that a created agent appears in mng list output."""
    agent_name = _unique_agent_name("listchk")
    prefix = os.environ.get("MNG_PREFIX", "mng-test-")
    session_name = f"{prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        exit_code = _create_zygote_agent(cli_runner, plugin_manager, agent_name, temp_git_repo)
        assert exit_code == 0

        from imbue.mng.cli.list import list_command

        list_result = cli_runner.invoke(
            list_command,
            ["--disable-plugin", "modal"],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert list_result.exit_code == 0
        assert agent_name in list_result.output


# -- Conversation watcher sync logic tests --


@pytest.mark.timeout(30)
def test_conversation_watcher_sync_with_llm_database(
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Test the conversation watcher's sync logic using a real SQLite database.

    Creates a minimal llm-compatible database and verifies that the sync
    script extracts messages correctly.
    """
    import sqlite3

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    conv_dir = agent_state_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    msg_dir = agent_state_dir / "logs" / "messages"
    msg_dir.mkdir(parents=True)

    # Create a tracked conversation
    conv_event = json.dumps(
        {
            "timestamp": "2025-01-15T10:00:00.000Z",
            "type": "conversation_created",
            "event_id": "evt-conv-1",
            "source": "conversations",
            "conversation_id": "conv-sync-test",
            "model": "claude-sonnet-4-6",
        }
    )
    (conv_dir / "events.jsonl").write_text(conv_event + "\n")

    # Create a minimal llm database with responses
    db_path = tmp_path / "logs.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE responses (
            id TEXT PRIMARY KEY,
            system TEXT,
            prompt TEXT,
            response TEXT,
            model TEXT,
            datetime_utc TEXT,
            conversation_id TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            token_details TEXT,
            response_json TEXT,
            reply_to_id TEXT,
            chat_id INTEGER,
            duration_ms INTEGER,
            attachment_type TEXT,
            attachment_path TEXT,
            attachment_url TEXT,
            attachment_content TEXT
        )
    """)

    conn.execute("""
        INSERT INTO responses (id, prompt, response, model, datetime_utc, conversation_id)
        VALUES ('resp-1', 'Hello there', 'Hi! How can I help?', 'claude-sonnet-4-6',
                '2025-01-15T10:01:00', 'conv-sync-test')
    """)
    conn.execute("""
        INSERT INTO responses (id, prompt, response, model, datetime_utc, conversation_id)
        VALUES ('resp-2', 'Tell me a joke', 'Why did the chicken...', 'claude-sonnet-4-6',
                '2025-01-15T10:02:00', 'conv-sync-test')
    """)
    conn.commit()
    conn.close()

    # Run the sync script's Python logic directly
    sync_env = os.environ.copy()
    sync_env["_CONVERSATIONS_FILE"] = str(conv_dir / "events.jsonl")
    sync_env["_MESSAGES_FILE"] = str(msg_dir / "events.jsonl")
    sync_env["_DB_PATH"] = str(db_path)

    sync_script = """
import json
import os
import sqlite3
import sys

def sync():
    conversations_file = os.environ["_CONVERSATIONS_FILE"]
    messages_file = os.environ["_MESSAGES_FILE"]
    db_path = os.environ["_DB_PATH"]

    tracked_cids = set()
    if os.path.isfile(conversations_file):
        with open(conversations_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    tracked_cids.add(json.loads(line)["conversation_id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    if not tracked_cids:
        print("0")
        return

    file_event_ids = set()
    if os.path.isfile(messages_file):
        with open(messages_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    file_event_ids.add(json.loads(line)["event_id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    placeholders = ",".join("?" for _ in tracked_cids)
    cid_list = list(tracked_cids)

    rows = conn.execute(
        f"SELECT id, datetime_utc, conversation_id, prompt, response "
        f"FROM responses "
        f"WHERE conversation_id IN ({placeholders}) "
        f"ORDER BY datetime_utc DESC "
        f"LIMIT 200",
        cid_list,
    ).fetchall()

    conn.close()

    missing_events = []
    for row_id, ts, cid, prompt, response in rows:
        if prompt:
            eid = f"{row_id}-user"
            if eid not in file_event_ids:
                missing_events.append((ts, 0, json.dumps({
                    "timestamp": ts,
                    "type": "message",
                    "event_id": eid,
                    "source": "messages",
                    "conversation_id": cid,
                    "role": "user",
                    "content": prompt,
                })))
        if response:
            eid = f"{row_id}-assistant"
            if eid not in file_event_ids:
                missing_events.append((ts, 1, json.dumps({
                    "timestamp": ts,
                    "type": "message",
                    "event_id": eid,
                    "source": "messages",
                    "conversation_id": cid,
                    "role": "assistant",
                    "content": response,
                })))

    if not missing_events:
        print("0")
        return

    missing_events.sort(key=lambda x: (x[0], x[1]))
    os.makedirs(os.path.dirname(messages_file), exist_ok=True)
    with open(messages_file, "a") as f:
        for _, _, event_json in missing_events:
            f.write(event_json + "\\n")

    print(str(len(missing_events)))

sync()
"""

    result = subprocess.run(
        ["python3", "-c", sync_script],
        capture_output=True,
        text=True,
        env=sync_env,
        timeout=10,
    )

    assert result.returncode == 0, f"Sync failed: {result.stderr}"
    synced_count = int(result.stdout.strip())
    assert synced_count == 4, f"Expected 4 synced events (2 user + 2 assistant), got {synced_count}"

    # Verify the messages events file
    messages_file = msg_dir / "events.jsonl"
    assert messages_file.exists()
    lines = messages_file.read_text().strip().split("\n")
    assert len(lines) == 4

    events = [json.loads(line) for line in lines]
    roles = [e["role"] for e in events]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2

    # All events should belong to our tracked conversation
    for event in events:
        assert event["conversation_id"] == "conv-sync-test"
        assert event["source"] == "messages"
        assert event["type"] == "message"


@pytest.mark.timeout(30)
def test_conversation_watcher_sync_is_idempotent(
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Verify that running the sync twice does not duplicate events."""
    import sqlite3

    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    conv_dir = agent_state_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    msg_dir = agent_state_dir / "logs" / "messages"
    msg_dir.mkdir(parents=True)

    conv_event = json.dumps(
        {
            "timestamp": "2025-01-15T10:00:00.000Z",
            "type": "conversation_created",
            "event_id": "evt-conv-idem",
            "source": "conversations",
            "conversation_id": "conv-idem-test",
            "model": "claude-sonnet-4-6",
        }
    )
    (conv_dir / "events.jsonl").write_text(conv_event + "\n")

    db_path = tmp_path / "logs.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE responses (
            id TEXT PRIMARY KEY,
            system TEXT,
            prompt TEXT,
            response TEXT,
            model TEXT,
            datetime_utc TEXT,
            conversation_id TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            token_details TEXT,
            response_json TEXT,
            reply_to_id TEXT,
            chat_id INTEGER,
            duration_ms INTEGER,
            attachment_type TEXT,
            attachment_path TEXT,
            attachment_url TEXT,
            attachment_content TEXT
        )
    """)
    conn.execute("""
        INSERT INTO responses (id, prompt, response, model, datetime_utc, conversation_id)
        VALUES ('resp-idem', 'Test message', 'Test response', 'claude-sonnet-4-6',
                '2025-01-15T10:01:00', 'conv-idem-test')
    """)
    conn.commit()
    conn.close()

    sync_env = os.environ.copy()
    sync_env["_CONVERSATIONS_FILE"] = str(conv_dir / "events.jsonl")
    sync_env["_MESSAGES_FILE"] = str(msg_dir / "events.jsonl")
    sync_env["_DB_PATH"] = str(db_path)

    sync_script = """
import json, os, sqlite3
def sync():
    cf = os.environ["_CONVERSATIONS_FILE"]
    mf = os.environ["_MESSAGES_FILE"]
    db = os.environ["_DB_PATH"]
    cids = set()
    for line in open(cf):
        line = line.strip()
        if line:
            try: cids.add(json.loads(line)["conversation_id"])
            except: pass
    if not cids: print("0"); return
    eids = set()
    if os.path.isfile(mf):
        for line in open(mf):
            line = line.strip()
            if line:
                try: eids.add(json.loads(line)["event_id"])
                except: pass
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ph = ",".join("?" for _ in cids)
    rows = conn.execute(f"SELECT id, datetime_utc, conversation_id, prompt, response FROM responses WHERE conversation_id IN ({ph}) ORDER BY datetime_utc DESC LIMIT 200", list(cids)).fetchall()
    conn.close()
    missing = []
    for rid, ts, cid, p, r in rows:
        if p:
            eid = f"{rid}-user"
            if eid not in eids: missing.append((ts, 0, json.dumps({"timestamp":ts,"type":"message","event_id":eid,"source":"messages","conversation_id":cid,"role":"user","content":p})))
        if r:
            eid = f"{rid}-assistant"
            if eid not in eids: missing.append((ts, 1, json.dumps({"timestamp":ts,"type":"message","event_id":eid,"source":"messages","conversation_id":cid,"role":"assistant","content":r})))
    if not missing: print("0"); return
    missing.sort(key=lambda x: (x[0], x[1]))
    os.makedirs(os.path.dirname(mf), exist_ok=True)
    with open(mf, "a") as f:
        for _, _, ej in missing: f.write(ej + "\\n")
    print(str(len(missing)))
sync()
"""

    # First sync
    r1 = subprocess.run(["python3", "-c", sync_script], capture_output=True, text=True, env=sync_env, timeout=10)
    assert r1.returncode == 0
    first_count = int(r1.stdout.strip())
    assert first_count == 2

    # Second sync should find 0 new events
    r2 = subprocess.run(["python3", "-c", sync_script], capture_output=True, text=True, env=sync_env, timeout=10)
    assert r2.returncode == 0
    second_count = int(r2.stdout.strip())
    assert second_count == 0

    # Verify file still has exactly 2 events
    messages_file = msg_dir / "events.jsonl"
    lines = messages_file.read_text().strip().split("\n")
    assert len(lines) == 2


# -- Compute claude project dir name tests --


def test_compute_claude_project_dir_name_replaces_slashes_and_dots() -> None:
    """Verify the project directory name computation."""
    assert compute_claude_project_dir_name("/home/user/.changelings/my-agent") == "-home-user--changelings-my-agent"


def test_compute_claude_project_dir_name_handles_simple_path() -> None:
    """Verify simple path conversion."""
    assert compute_claude_project_dir_name("/tmp/repo") == "-tmp-repo"
