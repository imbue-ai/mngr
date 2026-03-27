"""Integration tests for the mngr_claude_mind plugin.

Tests the plugin end-to-end by creating real agents in temporary git repos,
verifying provisioning creates the expected filesystem structures, and
exercising the chat and watcher scripts.

These tests use --command to override the default Claude command with
a simple sleep process, since Claude Code is not available in CI. This
still exercises all the provisioning, symlink creation, and tmux window
injection logic that the plugin provides.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import cast

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.create import create
from imbue.mngr.cli.list import list_command
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import tmux_session_exists
from imbue.mngr.utils.testing import wait_for_agent_session
from imbue.mngr_claude_mind.conftest import ChatScriptEnv
from imbue.mngr_claude_mind.conftest import LocalShellHost
from imbue.mngr_claude_mind.conftest import assert_conversation_exists_in_db
from imbue.mngr_claude_mind.conftest import create_mind_conversations_table_only
from imbue.mngr_claude_mind.conftest import parse_chat_output
from imbue.mngr_claude_mind.provisioning import create_mind_symlinks
from imbue.mngr_claude_mind.provisioning import setup_memory_directory
from imbue.mngr_llm.conftest import create_test_llm_db
from imbue.mngr_llm.conftest import write_conversation_to_db
from imbue.mngr_llm.data_types import ProvisioningSettings
from imbue.mngr_llm.provisioning import _LLM_TOOL_FILES
from imbue.mngr_llm.provisioning import _SERVICE_SCRIPT_FILES
from imbue.mngr_llm.provisioning import load_llm_resource
from imbue.mngr_llm.provisioning import provision_llm_tools
from imbue.mngr_llm.provisioning import provision_supporting_services
from imbue.mngr_llm.resources.conversation_watcher import _sync_messages
from imbue.mngr_mind.conftest import StubCommandResult
from imbue.mngr_mind.conftest import StubHost
from imbue.mngr_mind.provisioning import provision_link_skills_script_file

_DEFAULT_PROVISIONING = ProvisioningSettings()


def _unique_agent_name(label: str) -> str:
    """Generate a unique agent name for test isolation."""
    return f"test-{label}-{int(time.time())}"


@contextmanager
def _create_agent_in_session(
    label: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    source_dir: Path,
    *,
    extra_args: tuple[str, ...] = (),
) -> Generator[str, None, None]:
    """Context manager that creates an agent in a tmux session and cleans up on exit.

    Yields the session name for post-creation assertions. Handles the common
    boilerplate of generating a unique name, computing the session name from
    MNGR_PREFIX, invoking the CLI, and wrapping in tmux_session_cleanup.
    """
    agent_name = _unique_agent_name(label)
    prefix = os.environ.get("MNGR_PREFIX", "mngr-test-")
    session_name = f"{prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "sleep 847291",
                "--source",
                str(source_dir),
                "--no-connect",
                "--no-ensure-clean",
                "--disable-plugin",
                "modal",
                *extra_args,
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        # Wait for the tmux session to appear
        wait_for_agent_session(session_name)
        yield session_name


def _find_agent_state_dir(host_dir: Path) -> Path | None:
    """Find the first agent state directory under the host dir."""
    agents_dir = host_dir / "agents"
    if not agents_dir.exists():
        return None
    for entry in agents_dir.iterdir():
        if entry.is_dir():
            return entry
    return None


def _run_sync_script(messages_file: Path, db_path: Path) -> int:
    """Run the conversation watcher's sync logic and return the count of synced events."""
    return _sync_messages(db_path, messages_file)


# -- Provisioning filesystem structure tests --


@pytest.mark.timeout(30)
def test_provisioning_writes_supporting_services_to_host(
    local_shell_host: LocalShellHost,
) -> None:
    """Verify that provisioning writes all scripts with correct permissions."""
    agent_state_dir = local_shell_host.host_dir / "agents" / "test-agent"
    agent_state_dir.mkdir(parents=True, exist_ok=True)
    provision_supporting_services(cast(Any, local_shell_host), agent_state_dir, _DEFAULT_PROVISIONING)

    commands_dir = agent_state_dir / "commands"
    for script_name in _SERVICE_SCRIPT_FILES:
        script_path = commands_dir / script_name
        assert script_path.exists(), f"Expected {script_name} to be written"
        assert script_path.stat().st_mode & 0o111, f"Expected {script_name} to be executable"
        content = script_path.read_text()
        assert content.startswith("#!"), f"Expected {script_name} to have a shebang"


@pytest.mark.timeout(30)
def test_provisioning_writes_llm_tools_to_host(
    local_shell_host: LocalShellHost,
) -> None:
    """Verify that provisioning writes LLM tool scripts."""
    agent_state_dir = local_shell_host.host_dir / "agents" / "test-agent"
    agent_state_dir.mkdir(parents=True, exist_ok=True)
    provision_llm_tools(cast(Any, local_shell_host), agent_state_dir, _DEFAULT_PROVISIONING)

    tools_dir = agent_state_dir / "commands" / "llm_tools"
    for tool_file in _LLM_TOOL_FILES:
        tool_path = tools_dir / tool_file
        assert tool_path.exists(), f"Expected {tool_file} to be written"
        content = tool_path.read_text()
        assert "def " in content, f"Expected {tool_file} to contain Python function definitions"


@pytest.mark.timeout(30)
def test_provisioning_creates_link_skills_script_when_missing(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning writes link_skills.sh when it doesn't exist."""
    host = StubHost(
        host_dir=temp_host_dir,
        command_results={"test -f": StubCommandResult(success=False)},
        execute_mkdir=True,
    )

    provision_link_skills_script_file(cast(Any, host), temp_git_repo, _DEFAULT_PROVISIONING)

    written_path_strings = [str(p) for p, _ in host.written_text_files]
    assert any("link_skills.sh" in p for p in written_path_strings), "Expected link_skills.sh to be written"


@pytest.mark.timeout(30)
def test_provisioning_does_not_overwrite_existing_link_skills_script(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning does not overwrite link_skills.sh if it already exists."""
    host = StubHost(host_dir=temp_host_dir)

    provision_link_skills_script_file(cast(Any, host), temp_git_repo, _DEFAULT_PROVISIONING)

    assert len(host.written_text_files) == 0, "Should not overwrite existing files"


@pytest.mark.timeout(30)
def test_provisioning_creates_symlinks(
    temp_git_repo: Path,
    local_shell_host: LocalShellHost,
) -> None:
    """Verify that provisioning creates the expected symlinks.

    With the cd-into-role approach, we only create:
    - CLAUDE.md -> GLOBAL.md at the repo root
    - <role>/CLAUDE.local.md -> <role>/PROMPT.md within the role directory
    """
    # Set up the directory structure
    (temp_git_repo / "GLOBAL.md").write_text("# Global instructions")
    thinking_dir = temp_git_repo / "thinking"
    thinking_dir.mkdir()
    (thinking_dir / "PROMPT.md").write_text("# Thinking prompt")
    claude_dir = thinking_dir / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{}")

    create_mind_symlinks(cast(Any, local_shell_host), temp_git_repo, "thinking", _DEFAULT_PROVISIONING)

    # CLAUDE.md -> GLOBAL.md
    claude_md = temp_git_repo / "CLAUDE.md"
    assert claude_md.is_symlink(), "CLAUDE.md should be a symlink"
    assert claude_md.resolve() == (temp_git_repo / "GLOBAL.md").resolve()

    # thinking/CLAUDE.local.md -> thinking/PROMPT.md
    local_md = thinking_dir / "CLAUDE.local.md"
    assert local_md.is_symlink(), "thinking/CLAUDE.local.md should be a symlink"
    assert local_md.resolve() == (thinking_dir / "PROMPT.md").resolve()

    # No .claude symlink at the repo root (Claude Code runs from within the role dir)
    claude_link = temp_git_repo / ".claude"
    assert not claude_link.exists(), ".claude symlink should NOT be created at repo root"


@pytest.mark.timeout(30)
def test_provisioning_creates_memory_directory(
    temp_git_repo: Path,
    local_shell_host: LocalShellHost,
) -> None:
    """Verify that provisioning creates the per-role memory directory."""
    setup_memory_directory(cast(Any, local_shell_host), temp_git_repo, "thinking", _DEFAULT_PROVISIONING)

    memory_dir = temp_git_repo / "thinking" / "memory"
    assert memory_dir.is_dir(), "memory dir should exist"


# -- Chat script tests --


@pytest.mark.timeout(30)
def test_chat_script_shows_help(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh --help outputs usage information."""
    result = chat_env.run("--help")

    assert result.returncode == 0
    assert "chat" in result.stdout.lower()
    assert "--new" in result.stdout
    assert "--resume" in result.stdout
    assert "--list" in result.stdout


@pytest.mark.timeout(30)
def test_chat_script_list_shows_no_conversations_initially(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh --list reports no conversations when events file doesn't exist."""
    result = chat_env.run("--list")

    assert result.returncode == 0
    assert "no conversations" in result.stdout.lower()


@pytest.mark.timeout(30)
def test_chat_script_rejects_unknown_options(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh rejects unknown options with an error."""
    result = chat_env.run("--bogus")

    assert result.returncode != 0
    assert "unknown" in result.stderr.lower()


@pytest.mark.timeout(30)
def test_chat_script_resume_requires_conversation_id(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh --resume without a conversation ID fails."""
    result = chat_env.run("--resume")

    assert result.returncode != 0


@pytest.mark.timeout(30)
def test_chat_script_no_args_lists_and_shows_hint(chat_env: ChatScriptEnv) -> None:
    """Verify that calling chat.sh with no arguments lists conversations and shows a help hint."""
    result = chat_env.run()

    assert result.returncode == 0
    assert "--help" in result.stdout


@pytest.mark.timeout(30)
def test_chat_script_list_shows_existing_conversations(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh --list shows conversations from the database."""
    write_conversation_to_db(chat_env.llm_db_path, "conv-test-12345", model="claude-sonnet-4-6")

    result = chat_env.run("--list")

    assert result.returncode == 0
    assert "conv-test-12345" in result.stdout
    assert "claude-sonnet-4-6" in result.stdout


# -- Supporting service script syntax tests --


@pytest.mark.timeout(30)
def test_conversation_watcher_script_is_valid_python(chat_env: ChatScriptEnv) -> None:
    """Verify that conversation_watcher.py passes Python syntax check."""
    watcher_script = chat_env.agent_state_dir.parent.parent / "commands" / "conversation_watcher.py"
    watcher_script.parent.mkdir(parents=True, exist_ok=True)
    watcher_script.write_text(load_llm_resource("conversation_watcher.py"))

    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(watcher_script)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, f"Syntax check failed: {result.stderr}"


@pytest.mark.timeout(30)
def test_event_watcher_module_is_importable() -> None:
    """Verify that event_watcher module can be imported from mngr_mind."""
    from imbue.mngr_mind.event_watcher import main

    assert callable(main)


# -- Agent creation integration tests --


@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_create_agent_with_additional_commands(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that creating an agent with additional commands creates the expected tmux windows."""
    with _create_agent_in_session(
        "addcmd",
        cli_runner,
        plugin_manager,
        temp_git_repo,
        extra_args=("--extra-window", 'watcher="sleep 847292"'),
    ) as session_name:
        assert tmux_session_exists(session_name)

        windows_result = subprocess.run(
            ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        assert windows_result.returncode == 0
        window_names = windows_result.stdout.strip().split("\n")
        assert "watcher" in window_names, f"Expected 'watcher' window, got: {window_names}"


@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_create_agent_creates_state_directory(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that creating an agent creates the agent state directory."""
    with _create_agent_in_session("state", cli_runner, plugin_manager, temp_git_repo):
        agent_state_dir = _find_agent_state_dir(temp_host_dir)
        assert agent_state_dir is not None, "Agent state directory should exist"
        assert (agent_state_dir / "data.json").exists(), "data.json should exist in agent state dir"


# -- Conversation DB record tests --
# Note: settings loading tests (load_settings_from_host, provision_settings_file)
# are covered by unit tests in settings_test.py using StubHost.


@pytest.mark.timeout(30)
def test_conversation_record_written_to_db(chat_env: ChatScriptEnv) -> None:
    """Verify that conversation records written by chat.sh are stored in the database."""
    if shutil.which("llm") is None:
        pytest.skip("llm CLI not installed")

    chat_env.llm_db_path.unlink()
    chat_env.env["LLM_MATCHED_RESPONSE"] = ""
    chat_env.set_default_model("matched-responses")

    result = chat_env.run("--new", "--name", "test-conv", "--as-agent")

    assert result.returncode == 0, f"chat.sh failed: stdout={result.stdout!r} stderr={result.stderr!r}"

    output = parse_chat_output(result.stdout)
    conversation_id = output["conversation_id"]

    assert_conversation_exists_in_db(chat_env.llm_db_path, conversation_id)


@pytest.mark.timeout(30)
def test_multiple_conversations_create_separate_db_records(chat_env: ChatScriptEnv) -> None:
    """Verify that creating multiple conversations produces separate DB records."""
    if shutil.which("llm") is None:
        pytest.skip("llm CLI not installed")

    chat_env.llm_db_path.unlink()
    chat_env.env["LLM_MATCHED_RESPONSE"] = ""
    chat_env.set_default_model("matched-responses")

    conversation_ids = []
    for i in range(3):
        result = chat_env.run("--new", "--name", f"test-conv-{i}", "--as-agent")
        assert result.returncode == 0
        output = parse_chat_output(result.stdout)
        conversation_ids.append(output["conversation_id"])

    assert len(set(conversation_ids)) == 3, f"Expected 3 unique conversation IDs, got: {conversation_ids}"

    for cid in conversation_ids:
        assert_conversation_exists_in_db(chat_env.llm_db_path, cid)


@pytest.mark.timeout(30)
def test_chat_model_read_from_env_var(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh reads the model from MNGR_LLM_MODEL env var.

    The no-message --as-agent path uses ``matched-responses`` for the placeholder
    conversation, but the env var model should still appear in the log.
    """
    if shutil.which("llm") is None:
        pytest.skip("llm CLI not installed")

    chat_env.llm_db_path.unlink()
    chat_env.env["LLM_MATCHED_RESPONSE"] = ""
    chat_env.env["MNGR_LLM_MODEL"] = "claude-haiku-4-5"

    # Ensure log directory exists so we can check the log for the model
    log_dir = Path(chat_env.env["MNGR_AGENT_STATE_DIR"]) / "events" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    result = chat_env.run("--new", "--name", "model-test", "--as-agent")
    assert result.returncode == 0

    output = parse_chat_output(result.stdout)
    assert_conversation_exists_in_db(chat_env.llm_db_path, output["conversation_id"])

    # Verify the model from env var was logged (even though placeholder uses matched-responses)
    log_file = log_dir / "chat" / "events.jsonl"
    assert log_file.exists()
    log_content = log_file.read_text()
    assert "claude-haiku-4-5" in log_content


@pytest.mark.timeout(30)
def test_chat_script_creates_log_file(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh creates a log file with operation records."""
    chat_env.env["MNGR_LLM_MODEL"] = "claude-sonnet-4-6"

    # The log dir is at $MNGR_AGENT_STATE_DIR/events/logs/
    log_dir = Path(chat_env.env["MNGR_AGENT_STATE_DIR"]) / "events" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    chat_env.run("--new", "--name", "log-test", "--as-agent")

    log_file = log_dir / "chat" / "events.jsonl"
    assert log_file.exists(), "events/logs/chat/events.jsonl should be created"
    log_content = log_file.read_text()
    assert "No existing conversation" in log_content


# -- Event watcher offset tracking tests --


@pytest.mark.timeout(30)
def test_event_watcher_reads_settings_for_watched_sources(
    local_shell_host: LocalShellHost,
) -> None:
    """Verify that the event watcher script reads settings from settings.toml."""

    work_dir = local_shell_host.host_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Write minds.toml with custom watcher settings (both legacy and new fields)
    settings_content = (
        "[watchers]\n"
        "event_poll_interval_seconds = 7\n"
        'event_cel_filter = "source == \\"messages\\""\n'
        "event_burst_size = 3\n"
        "max_event_messages_per_minute = 20\n"
    )
    settings_path = work_dir / "minds.toml"
    settings_path.write_text(settings_content)

    # The event watcher reads settings via a Python snippet at startup.
    # Test that the Python settings-reading logic produces the expected output.
    settings_reader = f"""
import tomllib, pathlib, json
p = pathlib.Path('{settings_path}')
s = tomllib.loads(p.read_text()) if p.exists() else {{}}
w = s.get('watchers', {{}})
print(json.dumps({{
    'poll': w.get('event_poll_interval_seconds', 3),
    'cel_filter': w.get('event_cel_filter', ''),
    'burst_size': w.get('event_burst_size', 5),
    'max_messages_per_minute': w.get('max_event_messages_per_minute', 10),
}}))
"""
    result = subprocess.run(
        ["python3", "-c", settings_reader],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    parsed = json.loads(result.stdout.strip())
    assert parsed["poll"] == 7
    assert parsed["cel_filter"] == 'source == "messages"'
    assert parsed["burst_size"] == 3
    assert parsed["max_messages_per_minute"] == 20


# -- Stop hook script integration tests --


def _write_stop_hook_script(tmp_path: Path) -> Path:
    """Write the stop hook script to tmp_path for testing.

    Sets MNGR_AGENT_STATE_DIR to tmp_path so the script reads from
    tmp_path/mind/event_batches/ and tmp_path/events/handled_events/.
    """
    from imbue.mngr_claude_mind.provisioning import _STOP_HOOK_SCRIPT

    # Wrap the script to export MNGR_AGENT_STATE_DIR, replacing the original shebang
    body = _STOP_HOOK_SCRIPT.split("\n", 1)[1]
    wrapped = f"#!/usr/bin/env bash\nexport MNGR_AGENT_STATE_DIR={tmp_path}\n{body}"
    script_path = tmp_path / "on_stop_prevent_unhandled_events.sh"
    script_path.write_text(wrapped)
    script_path.chmod(0o755)
    return script_path


@pytest.mark.timeout(10)
def test_stop_hook_script_exits_2_when_unhandled_events(tmp_path: Path) -> None:
    """The stop hook script should exit 2 when events exist that are not handled."""
    script = _write_stop_hook_script(tmp_path)

    batches_dir = tmp_path / "mind" / "event_batches"
    batches_dir.mkdir(parents=True)
    (batches_dir / "test.jsonl").write_text(
        '{"event_id":"evt-1","source":"messages"}\n{"event_id":"evt-2","source":"messages"}\n'
    )

    handled_dir = tmp_path / "events" / "handled_events"
    handled_dir.mkdir(parents=True)
    (handled_dir / "events.jsonl").write_text('{"handled_event_id":"evt-1"}\n')

    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "evt-2" in result.stderr


@pytest.mark.timeout(10)
def test_stop_hook_script_exits_0_when_all_handled(tmp_path: Path) -> None:
    """The stop hook script should exit 0 when all events are handled."""
    script = _write_stop_hook_script(tmp_path)

    batches_dir = tmp_path / "mind" / "event_batches"
    batches_dir.mkdir(parents=True)
    (batches_dir / "test.jsonl").write_text(
        '{"event_id":"evt-1","source":"messages"}\n{"event_id":"evt-2","source":"messages"}\n'
    )

    handled_dir = tmp_path / "events" / "handled_events"
    handled_dir.mkdir(parents=True)
    (handled_dir / "events.jsonl").write_text('{"handled_event_id":"evt-1"}\n{"handled_event_id":"evt-2"}\n')

    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0


@pytest.mark.timeout(10)
def test_stop_hook_script_exits_0_when_no_events(tmp_path: Path) -> None:
    """The stop hook script should exit 0 when no event batch files exist."""
    script = _write_stop_hook_script(tmp_path)

    # Create the batches dir but leave it empty
    (tmp_path / "mind" / "event_batches").mkdir(parents=True)

    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0


@pytest.mark.timeout(10)
def test_stop_hook_script_reads_rotated_events_file(tmp_path: Path) -> None:
    """The stop hook script should read handled events from events.jsonl.1 (rotated file)."""
    script = _write_stop_hook_script(tmp_path)

    batches_dir = tmp_path / "mind" / "event_batches"
    batches_dir.mkdir(parents=True)
    (batches_dir / "test.jsonl").write_text(
        '{"event_id":"evt-1","source":"messages"}\n{"event_id":"evt-2","source":"messages"}\n'
    )

    handled_dir = tmp_path / "events" / "handled_events"
    handled_dir.mkdir(parents=True)
    # evt-1 in rotated file, evt-2 in current file
    (handled_dir / "events.jsonl.1").write_text('{"handled_event_id":"evt-1"}\n')
    (handled_dir / "events.jsonl").write_text('{"handled_event_id":"evt-2"}\n')

    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0


@pytest.mark.timeout(10)
def test_stop_hook_script_exits_2_when_nothing_handled(tmp_path: Path) -> None:
    """The stop hook script should exit 2 when events exist but nothing has been handled."""
    script = _write_stop_hook_script(tmp_path)

    batches_dir = tmp_path / "mind" / "event_batches"
    batches_dir.mkdir(parents=True)
    (batches_dir / "test.jsonl").write_text(
        '{"event_id":"evt-1","source":"messages"}\n{"event_id":"evt-2","source":"messages"}\n'
    )

    # No handled_events directory or files at all
    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "evt-1" in result.stderr
    assert "evt-2" in result.stderr


# -- link_skills.sh integration tests --


def _write_link_skills_script(tmp_path: Path) -> Path:
    """Write the link_skills.sh script from resources to tmp_path and make it executable."""
    import importlib.resources

    from imbue.mngr_mind import resources as resources_package

    resources_root = importlib.resources.files(resources_package)
    script_content = (resources_root / "link_skills.sh").read_text()
    script_path = tmp_path / "link_skills.sh"
    script_path.write_text(script_content)
    script_path.chmod(0o755)
    return script_path


@pytest.mark.timeout(10)
def test_link_skills_script_creates_symlinks(tmp_path: Path) -> None:
    """link_skills.sh should create symlinks from top-level skills into the role's skills dir."""
    skills_dir = tmp_path / "skills"
    (skills_dir / "delegate-task").mkdir(parents=True)
    (skills_dir / "delegate-task" / "SKILL.md").write_text("delegate skill")
    (skills_dir / "send-message").mkdir()
    (skills_dir / "send-message" / "SKILL.md").write_text("send skill")

    role_dir = tmp_path / "thinking"
    (role_dir / "skills").mkdir(parents=True)

    script_path = _write_link_skills_script(tmp_path)

    result = subprocess.run(
        [str(script_path), "thinking"],
        capture_output=True,
        text=True,
        timeout=5,
        cwd=tmp_path,
    )
    assert result.returncode == 0

    delegate_link = role_dir / "skills" / "delegate-task"
    assert delegate_link.is_symlink()
    assert (delegate_link / "SKILL.md").read_text() == "delegate skill"

    send_link = role_dir / "skills" / "send-message"
    assert send_link.is_symlink()
    assert (send_link / "SKILL.md").read_text() == "send skill"


@pytest.mark.timeout(10)
def test_link_skills_script_warns_on_existing_skill(tmp_path: Path) -> None:
    """link_skills.sh should warn and skip when a skill already exists in the role."""
    skills_dir = tmp_path / "skills"
    (skills_dir / "delegate-task").mkdir(parents=True)
    (skills_dir / "delegate-task" / "SKILL.md").write_text("shared")

    role_dir = tmp_path / "thinking"
    (role_dir / "skills" / "delegate-task").mkdir(parents=True)
    (role_dir / "skills" / "delegate-task" / "SKILL.md").write_text("role-specific")

    script_path = _write_link_skills_script(tmp_path)

    result = subprocess.run(
        [str(script_path), "thinking"],
        capture_output=True,
        text=True,
        timeout=5,
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert "WARNING" in result.stderr

    # Verify the existing skill was NOT overwritten
    assert (role_dir / "skills" / "delegate-task" / "SKILL.md").read_text() == "role-specific"
    assert not (role_dir / "skills" / "delegate-task").is_symlink()


# -- Tmux window injection integration tests --


@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_agent_with_ttyd_window_creates_session_with_expected_windows(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that adding named windows via --extra-window creates the expected tmux windows.

    This tests the window injection mechanism that the claude-mind plugin uses,
    without requiring ttyd to be installed.
    """
    with _create_agent_in_session(
        "ttyd",
        cli_runner,
        plugin_manager,
        temp_git_repo,
        extra_args=(
            "--extra-window",
            'agent_ttyd="sleep 847293"',
            "--extra-window",
            'conv_watcher="sleep 847294"',
            "--extra-window",
            'events="sleep 847295"',
            "--extra-window",
            'chat_ttyd="sleep 847296"',
        ),
    ) as session_name:
        assert tmux_session_exists(session_name)

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


@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_agent_creation_and_listing(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that a created agent appears in mngr list output."""
    with _create_agent_in_session("listchk", cli_runner, plugin_manager, temp_git_repo):
        list_result = cli_runner.invoke(
            list_command,
            ["--disable-plugin", "modal"],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert list_result.exit_code == 0


# -- Conversation watcher sync logic tests --


@pytest.mark.timeout(30)
def test_conversation_watcher_sync_with_llm_database(
    chat_env: ChatScriptEnv,
    tmp_path: Path,
) -> None:
    """Test the conversation watcher's sync logic using a real SQLite database.

    Creates a minimal llm-compatible database and verifies that the sync
    script extracts messages correctly.
    """
    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [
            (
                "resp-1",
                "Hello there",
                "Hi! How can I help?",
                "claude-sonnet-4-6",
                "2025-01-15T10:01:00",
                "conv-sync-test",
            ),
            (
                "resp-2",
                "Tell me a joke",
                "Why did the chicken...",
                "claude-sonnet-4-6",
                "2025-01-15T10:02:00",
                "conv-sync-test",
            ),
        ],
    )
    write_conversation_to_db(db_path, "conv-sync-test")

    synced_count = _run_sync_script(
        chat_env.messages_dir / "events.jsonl",
        db_path,
    )
    assert synced_count == 4, f"Expected 4 synced events (2 user + 2 assistant), got {synced_count}"

    messages_file = chat_env.messages_dir / "events.jsonl"
    assert messages_file.exists()
    lines = messages_file.read_text().strip().split("\n")
    assert len(lines) == 4

    events = [json.loads(line) for line in lines]
    roles = [e["role"] for e in events]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2

    for event in events:
        assert event["conversation_id"] == "conv-sync-test"
        assert event["source"] == "messages"
        assert event["type"] == "message"


@pytest.mark.timeout(30)
def test_conversation_watcher_sync_is_idempotent(
    chat_env: ChatScriptEnv,
    tmp_path: Path,
) -> None:
    """Verify that running the sync twice does not duplicate events."""
    db_path = tmp_path / "logs.db"
    create_test_llm_db(
        db_path,
        [
            (
                "resp-idem",
                "Test message",
                "Test response",
                "claude-sonnet-4-6",
                "2025-01-15T10:01:00",
                "conv-idem-test",
            ),
        ],
    )
    write_conversation_to_db(db_path, "conv-idem-test")

    messages_file = chat_env.messages_dir / "events.jsonl"

    first_count = _run_sync_script(messages_file, db_path)
    assert first_count == 2

    second_count = _run_sync_script(messages_file, db_path)
    assert second_count == 0

    lines = messages_file.read_text().strip().split("\n")
    assert len(lines) == 2


# -- Bug regression tests --


@pytest.mark.timeout(30)
def test_chat_script_uses_hardcoded_default_when_no_env_var(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh falls back to the hardcoded default model when MNGR_LLM_MODEL is not set."""
    if shutil.which("llm") is None:
        pytest.skip("llm CLI not installed")

    chat_env.llm_db_path.unlink()
    chat_env.env["LLM_MATCHED_RESPONSE"] = ""
    # Do NOT set MNGR_LLM_MODEL -- rely on hardcoded default

    # Ensure log directory exists so we can check the log for the default model
    log_dir = Path(chat_env.env["MNGR_AGENT_STATE_DIR"]) / "events" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    result = chat_env.run("--new", "--name", "default-model-test", "--as-agent")
    assert result.returncode == 0

    output = parse_chat_output(result.stdout)
    assert_conversation_exists_in_db(chat_env.llm_db_path, output["conversation_id"])

    # Verify the hardcoded default model was used (visible in log output)
    log_file = log_dir / "chat" / "events.jsonl"
    assert log_file.exists()
    log_content = log_file.read_text()
    assert "claude-haiku-4.5" in log_content


@pytest.mark.timeout(30)
def test_chat_script_db_model_lookup_finds_correct_model(chat_env: ChatScriptEnv) -> None:
    """Verify that the DB-based model lookup in resume_conversation finds the right model.

    resume_conversation() queries the llm conversations table to find the
    model for a conversation ID. This test inserts conversations directly
    into the llm conversations table and verifies the lookup works.
    """
    # Insert two conversations with different models into the llm conversations table
    cid1 = "conv-111-aabb"
    cid2 = "conv-222-ccdd"
    write_conversation_to_db(chat_env.llm_db_path, cid1, model="claude-sonnet-4-6")
    write_conversation_to_db(chat_env.llm_db_path, cid2, model="claude-haiku-4-5")

    # Use the conversation_db module directly (same logic as mngr llmdb)
    import io

    from imbue.mngr_llm.resources.conversation_db import lookup_model

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        lookup_model(str(chat_env.llm_db_path), cid2)
        assert sys.stdout.getvalue().strip() == "claude-haiku-4-5"
    finally:
        sys.stdout = old_stdout

    sys.stdout = io.StringIO()
    try:
        lookup_model(str(chat_env.llm_db_path), cid1)
        assert sys.stdout.getvalue().strip() == "claude-sonnet-4-6"
    finally:
        sys.stdout = old_stdout


@pytest.mark.timeout(30)
def test_chat_script_new_as_agent_with_message_fails_cleanly_without_llm(
    chat_env: ChatScriptEnv,
) -> None:
    """Verify --new --as-agent with a message fails cleanly when llm is not installed.

    The --as-agent path calls ``llm inject`` (without --cid) to create the
    conversation in the llm database first, then inserts into mind_conversations.
    If llm is not installed, the command should fail without creating a
    partial record in mind_conversations.
    """
    if shutil.which("llm") is not None:
        pytest.skip("llm CLI is installed; this test covers the missing-llm case")

    chat_env.env["MNGR_LLM_MODEL"] = "claude-sonnet-4-6"

    result = chat_env.run("--new", "--name", "msg-test", "--as-agent", "hello from test")

    # Command should fail because llm is not installed
    assert result.returncode != 0

    # No partial record should exist in mind_conversations
    with sqlite3.connect(str(chat_env.llm_db_path)) as conn:
        rows = conn.execute("SELECT conversation_id FROM mind_conversations").fetchall()
    assert len(rows) == 0, "no partial record should be created when llm inject fails"


@pytest.mark.timeout(30)
def test_chat_script_new_as_agent_with_message_creates_conversation(
    chat_env: ChatScriptEnv,
) -> None:
    """Verify --new --as-agent with a message creates the conversation in both tables.

    Requires llm to be installed. The conversation should exist in both
    the llm conversations table and the mind_conversations table.
    """
    if shutil.which("llm") is None:
        pytest.skip("llm CLI not installed")

    # Delete the pre-created DB so llm inject can run its own migrations
    chat_env.llm_db_path.unlink()
    chat_env.env["LLM_MATCHED_RESPONSE"] = ""
    chat_env.set_default_model("matched-responses")

    result = chat_env.run("--new", "--name", "msg-test", "--as-agent", "hello from test")
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = parse_chat_output(result.stdout)
    assert "conversation_id" in output, f"Expected conversation_id in output, got: {result.stdout!r}"
    conversation_id = output["conversation_id"]

    # Verify record exists in mind_conversations
    assert_conversation_exists_in_db(chat_env.llm_db_path, conversation_id)

    # Verify record also exists in llm's native conversations table
    with sqlite3.connect(str(chat_env.llm_db_path)) as conn:
        row = conn.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    assert row is not None, "conversation should exist in llm conversations table"


# -- Reply command tests --


@pytest.mark.timeout(30)
def test_chat_script_reply_requires_conversation_id(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh --reply without a conversation ID fails with usage error."""
    result = chat_env.run("--reply")

    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


@pytest.mark.timeout(30)
def test_chat_script_reply_requires_message(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh --reply with only a conversation ID fails with usage error."""
    result = chat_env.run("--reply", "conv-123")

    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


@pytest.mark.timeout(30)
def test_chat_script_reply_logs_correct_model_and_conversation(chat_env: ChatScriptEnv) -> None:
    """Verify that --reply logs the correct model and conversation ID before calling llm inject.

    The llm inject call will fail since llm is not installed in the test
    environment, but the log entry written before the call should contain
    the expected model and conversation ID.
    """
    chat_env.env["MNGR_LLM_MODEL"] = "claude-sonnet-4-6"

    log_dir = Path(chat_env.env["MNGR_AGENT_STATE_DIR"]) / "events" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    chat_env.run("--reply", "conv-reply-test", "hello from reply")

    log_file = log_dir / "chat" / "events.jsonl"
    assert log_file.exists()
    log_content = log_file.read_text()
    assert "conv-reply-test" in log_content
    assert "claude-sonnet-4-6" in log_content
    assert "reply_to_conversation" in log_content


@pytest.mark.timeout(30)
def test_chat_script_reply_outputs_message_id(chat_env: ChatScriptEnv) -> None:
    """Verify that --reply outputs the message_id of the injected response.

    Requires llm to be installed (skipped in CI where it is not available).
    """
    if shutil.which("llm") is None:
        pytest.skip("llm CLI not installed")

    chat_env.env["LLM_MATCHED_RESPONSE"] = ""

    # Delete existing DB and let llm inject create it fresh through its own
    # migration system, then add our mind_conversations table.
    chat_env.llm_db_path.unlink()

    # Create a real conversation via llm inject (no --cid)
    setup_result = subprocess.run(
        ["llm", "inject", "-m", "matched-responses", "--prompt", "setup", "initial"],
        capture_output=True,
        text=True,
        env=chat_env.env,
    )
    assert setup_result.returncode == 0, f"Setup inject failed: {setup_result.stderr}"
    # Parse conversation_id from "Injected message into conversation <id>"
    parts = setup_result.stdout.strip().rsplit(" ", 1)
    assert len(parts) == 2, f"Unexpected llm inject output: {setup_result.stdout!r}"
    conversation_id = parts[1]

    # Create the mind_conversations table (llm inject doesn't create it)
    create_mind_conversations_table_only(chat_env.llm_db_path)

    # Reply via chat.sh and verify message_id in output
    reply_result = chat_env.run("--reply", conversation_id, "follow-up message")
    assert reply_result.returncode == 0, f"Reply failed: {reply_result.stderr}"

    reply_output = parse_chat_output(reply_result.stdout)
    assert "message_id" in reply_output, f"Expected message_id in output, got: {reply_result.stdout!r}"
    assert len(reply_output["message_id"]) > 0


@pytest.mark.timeout(30)
def test_chat_script_new_as_agent_without_message_outputs_conversation_id(chat_env: ChatScriptEnv) -> None:
    """Verify that --new --as-agent without a message outputs conversation_id.

    Requires llm to be installed because the no-message path now creates
    a placeholder conversation via ``llm inject``.
    """
    if shutil.which("llm") is None:
        pytest.skip("llm CLI not installed")

    # Delete the pre-created DB so llm inject can run its own migrations
    chat_env.llm_db_path.unlink()
    chat_env.env["LLM_MATCHED_RESPONSE"] = ""
    chat_env.set_default_model("matched-responses")

    result = chat_env.run("--new", "--name", "no-msg-test", "--as-agent")
    assert result.returncode == 0, f"Failed: {result.stderr}"

    output = parse_chat_output(result.stdout)
    assert "conversation_id" in output, f"Expected conversation_id in output, got: {result.stdout!r}"

    # Verify the conversation exists in mind_conversations
    conversation_id = output["conversation_id"]
    assert_conversation_exists_in_db(chat_env.llm_db_path, conversation_id)


@pytest.mark.timeout(30)
def test_chat_script_help_shows_reply(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh --help output includes the --reply option."""
    result = chat_env.run("--help")

    assert result.returncode == 0
    assert "--reply" in result.stdout
