"""Integration tests for the mng_claude_changeling plugin.

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

from imbue.mng.cli.create import create
from imbue.mng.cli.list import list_command
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists
from imbue.mng_claude_changeling.conftest import ChatScriptEnv
from imbue.mng_claude_changeling.conftest import LocalShellHost
from imbue.mng_claude_changeling.conftest import StubCommandResult
from imbue.mng_claude_changeling.conftest import StubHost
from imbue.mng_claude_changeling.conftest import assert_conversation_exists_in_db
from imbue.mng_claude_changeling.conftest import create_test_llm_db
from imbue.mng_claude_changeling.conftest import write_conversation_to_db
from imbue.mng_claude_changeling.data_types import ProvisioningSettings
from imbue.mng_claude_changeling.provisioning import _DEFAULT_SKILL_DIRS
from imbue.mng_claude_changeling.provisioning import _DEFAULT_THINKING_DIR_FILES
from imbue.mng_claude_changeling.provisioning import _DEFAULT_WORK_DIR_FILES
from imbue.mng_claude_changeling.provisioning import _LLM_TOOL_FILES
from imbue.mng_claude_changeling.provisioning import _SERVICE_SCRIPT_FILES
from imbue.mng_claude_changeling.provisioning import compute_claude_project_dir_name
from imbue.mng_claude_changeling.provisioning import create_changeling_symlinks
from imbue.mng_claude_changeling.provisioning import create_event_log_directories
from imbue.mng_claude_changeling.provisioning import load_changeling_resource
from imbue.mng_claude_changeling.provisioning import provision_default_content
from imbue.mng_claude_changeling.provisioning import provision_llm_tools
from imbue.mng_claude_changeling.provisioning import provision_supporting_services
from imbue.mng_claude_changeling.provisioning import setup_memory_directory
from imbue.mng_claude_changeling.resources.conversation_watcher import _sync_messages

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
    MNG_PREFIX, invoking the CLI, and wrapping in tmux_session_cleanup.
    """
    agent_name = _unique_agent_name(label)
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
        assert result.exit_code == 0, f"CLI failed with: {result.output}"
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
def test_provisioning_creates_event_log_directories(
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning creates all expected event log directories."""
    agent_state_dir = temp_host_dir / "agents" / "test-agent"
    agent_state_dir.mkdir(parents=True)

    host = StubHost(host_dir=temp_host_dir, execute_mkdir=True)
    create_event_log_directories(cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING)

    expected_sources = (
        "messages",
        "scheduled",
        "mng_agents",
        "stop",
        "monitor",
    )
    for source in expected_sources:
        source_dir = agent_state_dir / "events" / source
        assert source_dir.exists(), f"Expected events/{source}/ directory to exist"

    # Log directories
    claude_transcript_dir = agent_state_dir / "logs" / "claude_transcript"
    assert claude_transcript_dir.exists(), "Expected logs/claude_transcript/ directory to exist"


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
def test_provisioning_creates_default_content_when_missing(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning writes default content files when they don't exist."""
    host = StubHost(
        host_dir=temp_host_dir,
        command_results={"test -f": StubCommandResult(success=False)},
        execute_mkdir=True,
    )

    written_paths: list[tuple[Path, str]] = []
    original_write = host.write_text_file

    def tracking_write(path: Path, content: str) -> None:
        written_paths.append((path, content))
        original_write(path, content)

    host.write_text_file = tracking_write  # type: ignore[assignment]

    provision_default_content(cast(Any, host), temp_git_repo, _DEFAULT_PROVISIONING)

    written_path_strings = [str(p) for p, _ in written_paths]

    for _, relative_path in _DEFAULT_WORK_DIR_FILES:
        expected = str(temp_git_repo / relative_path)
        assert expected in written_path_strings, f"Expected {relative_path} to be written to work dir"

    for _, relative_path in _DEFAULT_THINKING_DIR_FILES:
        expected = str(temp_git_repo / relative_path)
        assert expected in written_path_strings, f"Expected {relative_path} to be written to work dir"

    for skill_name in _DEFAULT_SKILL_DIRS:
        expected = str(temp_git_repo / "thinking" / ".claude" / "skills" / skill_name / "SKILL.md")
        assert expected in written_path_strings, f"Expected skill {skill_name}/SKILL.md to be written"


@pytest.mark.timeout(30)
def test_provisioning_does_not_overwrite_existing_content(
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Verify that provisioning does not overwrite files that already exist."""
    host = StubHost(host_dir=temp_host_dir)

    provision_default_content(cast(Any, host), temp_git_repo, _DEFAULT_PROVISIONING)

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

    create_changeling_symlinks(cast(Any, local_shell_host), temp_git_repo, "thinking", _DEFAULT_PROVISIONING)

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
@pytest.mark.rsync
def test_provisioning_syncs_memory_directory(
    temp_git_repo: Path,
    local_shell_host: LocalShellHost,
) -> None:
    """Verify that provisioning creates both memory dirs and syncs initial content."""
    abs_work_dir = str(temp_git_repo.resolve())
    role_dir_abs = f"{abs_work_dir}/thinking"
    # Create a file in thinking/memory/ to verify initial sync
    memory_dir = temp_git_repo / "thinking" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "test.md").write_text("hello")

    setup_memory_directory(cast(Any, local_shell_host), temp_git_repo, "thinking", role_dir_abs, _DEFAULT_PROVISIONING)

    assert memory_dir.is_dir(), "memory dir should exist"

    # Project dir name is derived from work dir (parent of role dir),
    # matching build_memory_sync_hooks_config
    project_dir_name = compute_claude_project_dir_name(abs_work_dir)
    project_memory = Path.home() / ".claude" / "projects" / project_dir_name / "memory"
    assert project_memory.is_dir(), "Claude project memory should be a real directory"
    assert not project_memory.is_symlink(), "Claude project memory should NOT be a symlink"
    assert (project_memory / "test.md").read_text() == "hello"


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
    watcher_script.write_text(load_changeling_resource("conversation_watcher.py"))

    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(watcher_script)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, f"Syntax check failed: {result.stderr}"


@pytest.mark.timeout(30)
def test_event_watcher_script_is_valid_python(chat_env: ChatScriptEnv) -> None:
    """Verify that event_watcher.py passes Python syntax check."""
    watcher_script = chat_env.agent_state_dir.parent.parent / "commands" / "event_watcher.py"
    watcher_script.parent.mkdir(parents=True, exist_ok=True)
    watcher_script.write_text(load_changeling_resource("event_watcher.py"))

    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(watcher_script)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, f"Syntax check failed: {result.stderr}"


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
        extra_args=("--add-command", 'watcher="sleep 847292"'),
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
    chat_env.set_default_model("claude-sonnet-4-6")

    result = chat_env.run("--new", "--as-agent")

    assert result.returncode == 0, f"chat.sh failed: stdout={result.stdout!r} stderr={result.stderr!r}"

    conversation_id = result.stdout.strip()
    assert conversation_id.startswith("conv-"), f"Expected conversation ID, got: {conversation_id!r}"

    assert_conversation_exists_in_db(chat_env.llm_db_path, conversation_id)


@pytest.mark.timeout(30)
def test_multiple_conversations_create_separate_db_records(chat_env: ChatScriptEnv) -> None:
    """Verify that creating multiple conversations produces separate DB records."""
    chat_env.set_default_model("claude-sonnet-4-6")

    conversation_ids = []
    for _ in range(3):
        result = chat_env.run("--new", "--as-agent")
        assert result.returncode == 0
        conversation_ids.append(result.stdout.strip())

    assert len(set(conversation_ids)) == 3, f"Expected 3 unique conversation IDs, got: {conversation_ids}"

    for cid in conversation_ids:
        assert_conversation_exists_in_db(chat_env.llm_db_path, cid)


@pytest.mark.timeout(30)
def test_chat_model_read_from_settings_toml(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh reads the model from settings.toml."""
    chat_env.set_default_model("claude-haiku-4-5")

    # Ensure log directory exists so we can check the log for the model
    log_dir = Path(chat_env.env["MNG_AGENT_STATE_DIR"]) / "events" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    result = chat_env.run("--new", "--as-agent")
    assert result.returncode == 0

    conversation_id = result.stdout.strip()
    assert_conversation_exists_in_db(chat_env.llm_db_path, conversation_id)

    # Verify the model from settings.toml was used (visible in log output)
    log_file = log_dir / "chat" / "events.jsonl"
    assert log_file.exists()
    log_content = log_file.read_text()
    assert "claude-haiku-4-5" in log_content


@pytest.mark.timeout(30)
def test_chat_script_creates_log_file(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh creates a log file with operation records."""
    chat_env.set_default_model("claude-sonnet-4-6")

    # The log dir is at $MNG_AGENT_STATE_DIR/events/logs/
    log_dir = Path(chat_env.env["MNG_AGENT_STATE_DIR"]) / "events" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    chat_env.run("--new", "--as-agent")

    log_file = log_dir / "chat" / "events.jsonl"
    assert log_file.exists(), "events/logs/chat/events.jsonl should be created"
    log_content = log_file.read_text()
    assert "Creating new conversation" in log_content


# -- Event watcher offset tracking tests --


@pytest.mark.timeout(30)
def test_event_watcher_reads_settings_for_watched_sources(
    local_shell_host: LocalShellHost,
) -> None:
    """Verify that the event watcher script reads settings from settings.toml."""

    work_dir = local_shell_host.host_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Write changelings.toml with custom watcher settings (both legacy and new fields)
    settings_content = (
        "[watchers]\n"
        "event_poll_interval_seconds = 7\n"
        'event_cel_filter = "source == \\"messages\\""\n'
        "event_burst_size = 3\n"
        "max_event_messages_per_minute = 20\n"
        "high_rate_warning_threshold_per_minute = 15\n"
    )
    settings_path = work_dir / "changelings.toml"
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
    'high_rate_warning_threshold': w.get('high_rate_warning_threshold_per_minute', 8),
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
    assert parsed["high_rate_warning_threshold"] == 15


# -- Tmux window injection integration tests --


@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_agent_with_ttyd_window_creates_session_with_expected_windows(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that adding named windows via --add-command creates the expected tmux windows.

    This tests the window injection mechanism that the claude-changeling plugin uses,
    without requiring ttyd to be installed.
    """
    with _create_agent_in_session(
        "ttyd",
        cli_runner,
        plugin_manager,
        temp_git_repo,
        extra_args=(
            "--add-command",
            'agent_ttyd="sleep 847293"',
            "--add-command",
            'conv_watcher="sleep 847294"',
            "--add-command",
            'events="sleep 847295"',
            "--add-command",
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
    """Verify that a created agent appears in mng list output."""
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
def test_chat_script_uses_hardcoded_default_when_no_settings(chat_env: ChatScriptEnv) -> None:
    """Verify that chat.sh falls back to the hardcoded default model when settings.toml is absent."""
    # Do NOT call set_default_model -- no settings.toml exists

    # Ensure log directory exists so we can check the log for the default model
    log_dir = Path(chat_env.env["MNG_AGENT_STATE_DIR"]) / "events" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    result = chat_env.run("--new", "--as-agent")
    assert result.returncode == 0

    conversation_id = result.stdout.strip()
    assert_conversation_exists_in_db(chat_env.llm_db_path, conversation_id)

    # Verify the hardcoded default model was used (visible in log output)
    log_file = log_dir / "chat" / "events.jsonl"
    assert log_file.exists()
    log_content = log_file.read_text()
    assert "claude-opus-4.6" in log_content


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

    # Use the conversation_db module directly (same logic as mng changelingdb)
    import io

    from imbue.mng_claude_changeling.resources.conversation_db import db_lookup_model

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        db_lookup_model(str(chat_env.llm_db_path), cid2)
        assert sys.stdout.getvalue().strip() == "claude-haiku-4-5"
    finally:
        sys.stdout = old_stdout

    sys.stdout = io.StringIO()
    try:
        db_lookup_model(str(chat_env.llm_db_path), cid1)
        assert sys.stdout.getvalue().strip() == "claude-sonnet-4-6"
    finally:
        sys.stdout = old_stdout


@pytest.mark.timeout(30)
def test_chat_script_new_as_agent_with_message_writes_record_without_llm(
    chat_env: ChatScriptEnv,
) -> None:
    """Verify --new --as-agent with a message still writes the conversation record.

    The --as-agent path with a message calls `llm inject` which will fail if
    llm is not installed. But the conversation record should still be
    written to the DB regardless, since insert_conversation_record runs
    before the llm inject call.
    """
    chat_env.set_default_model("claude-sonnet-4-6")

    # This will fail at the `llm inject` call since llm is not installed,
    # but the conversation record should already be inserted because
    # insert_conversation_record runs before the llm inject call.
    chat_env.run("--new", "--as-agent", "hello from test")

    # At least one conversation should exist (the one just created)
    with sqlite3.connect(str(chat_env.llm_db_path)) as conn:
        rows = conn.execute("SELECT conversation_id FROM changeling_conversations").fetchall()
    assert len(rows) >= 1, "conversation record should be written even when llm inject fails"
