"""Unit tests for the mngr_claude_mind provisioning module."""

import importlib
import os
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude_mind.provisioning import build_stop_hook_config
from imbue.mngr_claude_mind.provisioning import create_mind_symlinks
from imbue.mngr_claude_mind.provisioning import provision_claude_settings
from imbue.mngr_claude_mind.provisioning import provision_event_exclude_sources
from imbue.mngr_claude_mind.provisioning import provision_stop_hook_script
from imbue.mngr_claude_mind.provisioning import run_link_skills_script
from imbue.mngr_claude_mind.provisioning import setup_memory_directory
from imbue.mngr_llm.conftest import create_mind_conversations_table_in_test_db
from imbue.mngr_llm.conftest import write_conversation_to_db
from imbue.mngr_llm.data_types import DEFAULT_WELCOME_MESSAGE
from imbue.mngr_llm.data_types import ProvisioningSettings
from imbue.mngr_llm.provisioning import MIND_CONVERSATIONS_TABLE_SQL
from imbue.mngr_llm.provisioning import _LLM_TOOL_FILES
from imbue.mngr_llm.provisioning import _SERVICE_SCRIPT_FILES
from imbue.mngr_llm.provisioning import _TTYD_DISPATCH_SCRIPTS
from imbue.mngr_llm.provisioning import configure_llm_user_path
from imbue.mngr_llm.provisioning import create_first_daily_conversation
from imbue.mngr_llm.provisioning import create_slack_notifications_conversation
from imbue.mngr_llm.provisioning import create_system_notifications_conversation
from imbue.mngr_llm.provisioning import create_work_log_conversation
from imbue.mngr_llm.provisioning import install_llm_toolchain
from imbue.mngr_llm.provisioning import load_llm_resource
from imbue.mngr_llm.provisioning import provision_llm_tools
from imbue.mngr_llm.provisioning import provision_supporting_services
from imbue.mngr_llm.provisioning import resolve_work_dir_abs
from imbue.mngr_llm.resources import context_tool as context_tool_module
from imbue.mngr_llm.resources import extra_context_tool as extra_context_tool_module
from imbue.mngr_mind.conftest import StubCommandResult
from imbue.mngr_mind.conftest import StubHost
from imbue.mngr_mind.provisioning import provision_link_skills_script_file
from imbue.mngr_recursive.watcher_common import MngrNotInstalledError
from imbue.mngr_recursive.watcher_common import get_mngr_command

_DEFAULT_PROVISIONING = ProvisioningSettings()


# -- provision_claude_settings tests --


def test_provision_claude_settings_writes_when_missing() -> None:
    """Verify provision_claude_settings writes settings.json when it doesn't exist."""
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    provision_claude_settings(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    written_paths = [str(p) for p, _ in host.written_text_files]
    assert any("thinking/.claude/settings.json" in p for p in written_paths)


def test_provision_claude_settings_does_not_overwrite() -> None:
    """Verify provision_claude_settings skips when file exists."""
    host = StubHost()
    provision_claude_settings(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    assert len(host.written_text_files) == 0


# -- Memory directory tests --


def test_encode_claude_project_dir_name_replaces_slashes() -> None:
    assert encode_claude_project_dir_name(Path("/home/user/project")) == "-home-user-project"


def test_encode_claude_project_dir_name_replaces_dots() -> None:
    assert encode_claude_project_dir_name(Path("/home/user/.minds/agent")) == "-home-user--minds-agent"


def _run_setup_memory(
    work_dir: str = "/home/user/.minds/agent",
    active_role: str = "thinking",
) -> StubHost:
    """Run setup_memory_directory on a StubHost and return the host for inspection."""
    host = StubHost()
    setup_memory_directory(cast(Any, host), Path(work_dir), active_role, _DEFAULT_PROVISIONING)
    return host


def test_setup_memory_directory_creates_dir() -> None:
    host = _run_setup_memory()
    assert any("mkdir" in c and "/memory" in c for c in host.executed_commands)


def test_setup_memory_directory_no_rsync() -> None:
    """Memory sync is handled by autoMemoryDirectory, not rsync hooks."""
    host = _run_setup_memory()
    assert not any("rsync" in c for c in host.executed_commands)


def test_setup_memory_directory_no_claude_project_dir() -> None:
    """Claude project memory dir is not created; autoMemoryDirectory handles memory location."""
    host = _run_setup_memory()
    assert not any(".claude/projects" in c for c in host.executed_commands)


# -- build_stop_hook_config tests --


def test_build_stop_hook_config_references_script_path() -> None:
    script_path = Path("/test/work/thinking/.claude/hooks/on_stop_prevent_unhandled_events.sh")
    config = build_stop_hook_config(script_path)
    assert "hooks" in config
    assert "Stop" in config["hooks"]
    hook_command = config["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert hook_command == str(script_path)


# -- provision_stop_hook_script tests --


def test_provision_stop_hook_script_writes_and_chmods() -> None:
    host = StubHost()
    path = provision_stop_hook_script(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)
    assert path == Path("/test/work/thinking/.claude/hooks/on_stop_prevent_unhandled_events.sh")
    assert any("mkdir -p" in c and "hooks" in c for c in host.executed_commands)
    assert any("chmod +x" in c for c in host.executed_commands)
    assert len(host.written_text_files) == 1
    written_path, content = host.written_text_files[0]
    assert "on_stop_prevent_unhandled_events.sh" in str(written_path)
    assert "#!/usr/bin/env bash" in content
    assert "handled_event_id" in content
    assert "event_batches" in content


# -- run_link_skills_script tests --


def test_run_link_skills_script_skips_when_script_missing() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    run_link_skills_script(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    assert not any("chmod" in c for c in host.executed_commands)


def test_run_link_skills_script_runs_when_script_exists() -> None:
    host = StubHost()
    run_link_skills_script(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    assert any("chmod +x" in c for c in host.executed_commands)
    assert any("link_skills.sh" in c and "thinking" in c for c in host.executed_commands)


def test_run_link_skills_script_passes_role_as_argument() -> None:
    host = StubHost()
    run_link_skills_script(cast(Any, host), Path("/test/work"), "custom-role", _DEFAULT_PROVISIONING)

    assert any("custom-role" in c for c in host.executed_commands)


# -- Provisioning function tests (using _StubHost) --


def test_install_llm_toolchain_skips_when_already_present() -> None:
    host = StubHost()
    install_llm_toolchain(cast(Any, host), _DEFAULT_PROVISIONING)

    assert any("command -v llm" in c for c in host.executed_commands)
    assert not any("uv tool install llm" in c for c in host.executed_commands)


def test_install_llm_toolchain_installs_when_missing() -> None:
    host = StubHost(command_results={"command -v llm": StubCommandResult(success=False)})
    install_llm_toolchain(cast(Any, host), _DEFAULT_PROVISIONING)

    assert any("uv tool install llm" in c for c in host.executed_commands)


def test_install_llm_toolchain_installs_anthropic_plugin() -> None:
    host = StubHost()
    install_llm_toolchain(cast(Any, host), _DEFAULT_PROVISIONING)

    assert any("llm install llm-anthropic" in c for c in host.executed_commands)


def test_install_llm_toolchain_installs_live_chat_plugin() -> None:
    host = StubHost()
    install_llm_toolchain(cast(Any, host), _DEFAULT_PROVISIONING)

    assert any("llm install llm-live-chat" in c for c in host.executed_commands)


def test_install_llm_toolchain_raises_on_llm_install_failure() -> None:
    host = StubHost(
        command_results={
            "command -v llm": StubCommandResult(success=False),
            "uv tool install llm": StubCommandResult(success=False, stderr="install failed"),
        }
    )
    with pytest.raises(RuntimeError, match="Failed to install llm"):
        install_llm_toolchain(cast(Any, host), _DEFAULT_PROVISIONING)


def test_install_llm_toolchain_raises_on_plugin_install_failure() -> None:
    host = StubHost(
        command_results={"llm install llm-anthropic": StubCommandResult(success=False, stderr="plugin failed")}
    )
    with pytest.raises(RuntimeError, match="Failed to install llm-anthropic"):
        install_llm_toolchain(cast(Any, host), _DEFAULT_PROVISIONING)


def test_create_mind_symlinks_checks_global_md() -> None:
    host = StubHost()
    create_mind_symlinks(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    assert any("GLOBAL.md" in c for c in host.executed_commands)


def test_create_mind_symlinks_checks_thinking_prompt() -> None:
    host = StubHost()
    create_mind_symlinks(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    assert any("thinking/PROMPT.md" in c for c in host.executed_commands)


def test_create_mind_symlinks_creates_claude_md() -> None:
    host = StubHost()
    create_mind_symlinks(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    assert any("ln -sf" in c and "CLAUDE.md" in c for c in host.executed_commands)


def test_create_mind_symlinks_creates_claude_local_md() -> None:
    host = StubHost()
    create_mind_symlinks(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    assert any("ln -sf" in c and "CLAUDE.local.md" in c for c in host.executed_commands)


def test_create_mind_symlinks_creates_skills_symlink() -> None:
    """Verify that .claude/skills is symlinked to skills."""
    host = StubHost()
    create_mind_symlinks(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    assert any("ln -sf" in c and ".claude/skills" in c for c in host.executed_commands)
    assert any("thinking/skills" in c for c in host.executed_commands)


def test_provision_link_skills_script_file_writes_when_missing() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    provision_link_skills_script_file(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(p) for p, _ in host.written_text_files]
    assert any("link_skills.sh" in p for p in written_paths)


def test_provision_link_skills_script_file_skips_when_existing() -> None:
    host = StubHost()
    provision_link_skills_script_file(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert len(host.written_text_files) == 0


def test_provision_supporting_services_creates_commands_and_ttyd_dirs() -> None:
    host = StubHost()
    provision_supporting_services(cast(Any, host), Path("/tmp/mngr-test/agents/agent-123"), _DEFAULT_PROVISIONING)

    assert any("mkdir" in c and "commands/ttyd" in c for c in host.executed_commands)


def test_provision_supporting_services_writes_all_scripts() -> None:
    host = StubHost()
    provision_supporting_services(cast(Any, host), Path("/tmp/mngr-test/agents/agent-123"), _DEFAULT_PROVISIONING)

    written_names = [str(path) for path, _, _ in host.written_files]
    for script_name in _SERVICE_SCRIPT_FILES:
        assert any(script_name in name for name in written_names), f"{script_name} not written"


def test_provision_supporting_services_uses_executable_mode() -> None:
    host = StubHost()
    provision_supporting_services(cast(Any, host), Path("/tmp/mngr-test/agents/agent-123"), _DEFAULT_PROVISIONING)

    for path, _, mode in host.written_files:
        assert mode == "0755", f"Expected 0755 for script {path.name}, got {mode}"


def test_provision_supporting_services_writes_ttyd_dispatch_scripts() -> None:
    """Verify that ttyd dispatch scripts are written to commands/ttyd/."""
    host = StubHost()
    provision_supporting_services(cast(Any, host), Path("/tmp/mngr-test/agents/agent-123"), _DEFAULT_PROVISIONING)

    written_names = [str(path) for path, _, _ in host.written_files]
    for _, target_name in _TTYD_DISPATCH_SCRIPTS:
        expected_suffix = f"commands/ttyd/{target_name}"
        assert any(expected_suffix in name for name in written_names), f"ttyd/{target_name} not written"


def test_provision_llm_tools_creates_tools_dir() -> None:
    host = StubHost()
    provision_llm_tools(cast(Any, host), Path("/tmp/mngr-test/agents/agent-123"), _DEFAULT_PROVISIONING)

    assert any("mkdir" in c and "llm_tools" in c for c in host.executed_commands)


def test_provision_llm_tools_writes_all_tool_files() -> None:
    host = StubHost()
    provision_llm_tools(cast(Any, host), Path("/tmp/mngr-test/agents/agent-123"), _DEFAULT_PROVISIONING)

    written_names = [str(path) for path, _, _ in host.written_files]
    for tool_file in _LLM_TOOL_FILES:
        assert any(tool_file in name for name in written_names), f"{tool_file} not written"


# -- Schema sync tests --


def test_conversation_db_schema_matches_provisioning() -> None:
    """Verify conversation_db.py contains all column definitions from provisioning.py's schema.

    conversation_db.py runs standalone on remote hosts and cannot import from
    provisioning.py, so the schema is duplicated. This test catches drift by
    checking that every column definition from the authoritative schema appears
    in the resource file source.
    """
    source = load_llm_resource("conversation_db.py")
    # Extract column definitions from the authoritative schema constant.
    # Each "X TEXT ..." clause must appear in conversation_db.py's source.
    for fragment in MIND_CONVERSATIONS_TABLE_SQL.split("(", 1)[1].rsplit(")", 1)[0].split(","):
        fragment = fragment.strip()
        assert fragment in source, (
            f"conversation_db.py is missing schema fragment {fragment!r} from "
            "provisioning.py MIND_CONVERSATIONS_TABLE_SQL. "
            "These must be kept in sync."
        )


# -- configure_llm_user_path tests --


def test_configure_llm_user_path_creates_dir() -> None:
    host = StubHost()
    agent_state_dir = Path("/tmp/mngr-test/agents/agent-123")
    configure_llm_user_path(cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING)

    # Should create llm_data directory
    assert any("llm_data" in c and "mkdir" in c for c in host.executed_commands)


# -- internal conversation tests (system_notifications, slack_notifications) --


_FAKE_INJECT_RESULT = StubCommandResult(
    stdout="Injected message into conversation fake-conv-id-123\n",
)


@pytest.mark.parametrize(
    ("create_fn", "expected_tag"),
    [
        (create_system_notifications_conversation, "system_notifications"),
        (create_slack_notifications_conversation, "slack_notifications"),
    ],
    ids=["system_notifications", "slack_notifications"],
)
def test_create_internal_conversation_runs_inject_and_records_event(
    create_fn: Any,
    expected_tag: str,
) -> None:
    host = StubHost(command_results={"llm inject": _FAKE_INJECT_RESULT})
    agent_state_dir = Path("/tmp/mngr-test/agents/agent-123")
    create_fn(cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING)

    # Should run llm inject with LLM_USER_PATH prefix (no --cid, llm assigns the ID)
    inject_commands = [c for c in host.executed_commands if "llm inject" in c]
    assert len(inject_commands) == 1
    assert "--cid" not in inject_commands[0]
    assert "LLM_USER_PATH=" in inject_commands[0]
    assert "llm_data" in inject_commands[0]

    # Should insert a record into mind_conversations via sqlite3
    db_commands = [c for c in host.executed_commands if "sqlite3" in c and "mind_conversations" in c]
    assert len(db_commands) == 1
    assert "fake-conv-id-123" in db_commands[0]
    assert "internal" in db_commands[0]
    assert expected_tag in db_commands[0]


@pytest.mark.parametrize(
    "create_fn",
    [create_system_notifications_conversation, create_slack_notifications_conversation],
    ids=["system_notifications", "slack_notifications"],
)
def test_create_internal_conversation_skips_event_on_inject_failure(
    create_fn: Any,
) -> None:
    host = StubHost(
        command_results={"llm inject": StubCommandResult(success=False, stderr="llm not found")},
    )
    agent_state_dir = Path("/tmp/mngr-test/agents/agent-123")
    create_fn(cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING)

    # Should have attempted llm inject
    inject_commands = [c for c in host.executed_commands if "llm inject" in c]
    assert len(inject_commands) == 1

    # Should NOT have written a DB record (early return on failure)
    db_commands = [c for c in host.executed_commands if "sqlite3" in c and "mind_conversations" in c]
    assert len(db_commands) == 0


# -- create_daily_conversation tests --


def test_create_daily_conversation_runs_inject_and_records_tagged_event() -> None:
    host = StubHost(command_results={"llm inject": _FAKE_INJECT_RESULT})
    agent_state_dir = Path("/tmp/mngr-test/agents/agent-123")
    create_first_daily_conversation(
        cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING, "claude-opus-4.6", DEFAULT_WELCOME_MESSAGE
    )

    # Should run llm inject with the greeting and LLM_USER_PATH
    inject_commands = [c for c in host.executed_commands if "llm inject" in c]
    assert len(inject_commands) == 1
    assert str(DEFAULT_WELCOME_MESSAGE) in inject_commands[0]
    assert "claude-opus-4.6" in inject_commands[0]
    assert "LLM_USER_PATH=" in inject_commands[0]

    # Should insert a record with daily tag into mind_conversations via sqlite3
    db_commands = [c for c in host.executed_commands if "sqlite3" in c and "mind_conversations" in c]
    assert len(db_commands) == 1
    assert "daily" in db_commands[0]
    assert "fake-conv-id-123" in db_commands[0]


def test_create_daily_conversation_uses_custom_welcome_message() -> None:
    host = StubHost(command_results={"llm inject": _FAKE_INJECT_RESULT})
    agent_state_dir = Path("/tmp/mngr-test/agents/agent-123")
    custom_message = "Welcome! How can I help you today?"
    create_first_daily_conversation(
        cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING, "claude-opus-4.6", custom_message
    )

    inject_commands = [c for c in host.executed_commands if "llm inject" in c]
    assert len(inject_commands) == 1
    assert custom_message in inject_commands[0]
    # Verify the default message is NOT present
    assert "Selene" not in inject_commands[0]


# -- create_work_log_conversation tests --


def test_create_work_log_conversation_runs_inject_and_records_tagged_event() -> None:
    host = StubHost(command_results={"llm inject": _FAKE_INJECT_RESULT})
    agent_state_dir = Path("/tmp/mngr-test/agents/agent-123")
    create_work_log_conversation(cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING, "claude-opus-4.6")

    inject_commands = [c for c in host.executed_commands if "llm inject" in c]
    assert len(inject_commands) == 1
    assert "claude-opus-4.6" in inject_commands[0]
    assert "LLM_USER_PATH=" in inject_commands[0]
    assert "Work log initialized" in inject_commands[0]

    db_commands = [c for c in host.executed_commands if "sqlite3" in c and "mind_conversations" in c]
    assert len(db_commands) == 1
    assert "fake-conv-id-123" in db_commands[0]
    assert "Work Log" in db_commands[0]
    assert "work_log" in db_commands[0]

    # Verify the conversation name is updated in the llm conversations table
    name_update_commands = [
        c for c in host.executed_commands if "sqlite3" in c and "UPDATE conversations" in c
    ]
    assert len(name_update_commands) == 1
    assert "Work Log" in name_update_commands[0]
    assert "fake-conv-id-123" in name_update_commands[0]


def test_create_work_log_conversation_skips_event_on_inject_failure() -> None:
    host = StubHost(
        command_results={"llm inject": StubCommandResult(success=False, stderr="llm not found")},
    )
    agent_state_dir = Path("/tmp/mngr-test/agents/agent-123")
    create_work_log_conversation(cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING, "claude-opus-4.6")

    db_commands = [c for c in host.executed_commands if "sqlite3" in c and "mind_conversations" in c]
    assert len(db_commands) == 0


def test_create_daily_conversation_skips_event_on_inject_failure() -> None:
    host = StubHost(
        command_results={"llm inject": StubCommandResult(success=False, stderr="llm not found")},
    )
    agent_state_dir = Path("/tmp/mngr-test/agents/agent-123")
    create_first_daily_conversation(
        cast(Any, host), agent_state_dir, _DEFAULT_PROVISIONING, "claude-opus-4.6", DEFAULT_WELCOME_MESSAGE
    )

    # Should NOT have written a DB record
    db_commands = [c for c in host.executed_commands if "sqlite3" in c and "mind_conversations" in c]
    assert len(db_commands) == 0


# -- context_tool incremental behavior tests --


def _load_fresh_context_tool(name: str) -> Any:
    """Import context_tool as a proper package module and reset its state.

    Uses a real package import (so coverage can track execution) and
    importlib.reload to reinitialize module-level state like _last_file_sizes.
    The ``name`` parameter is accepted for backward-compatibility but unused.
    """
    importlib.reload(context_tool_module)
    return context_tool_module


def test_context_tool_gather_context_returns_no_context_when_env_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns a message when MNGR_AGENT_STATE_DIR is not set."""
    module = _load_fresh_context_tool("context_tool_test_module")
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)

    result = module.gather_context()
    assert "No agent data directory" in result


def test_context_tool_gather_context_returns_no_new_context_on_second_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns incremental results on subsequent calls."""
    # Set up a minimal agent data dir with one scheduled event
    events_source_dir = tmp_path / "events" / "scheduled"
    events_source_dir.mkdir(parents=True)
    events_file = events_source_dir / "events.jsonl"
    events_file.write_text('{"timestamp":"2026-01-01T00:00:00Z","type":"test","event_id":"e1","source":"scheduled"}\n')

    module = _load_fresh_context_tool("context_tool_incremental_test")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    # First call: should return the event
    first_result = module.gather_context()
    assert "scheduled" in first_result.lower()

    # Second call with no new events: should report no new context
    second_result = module.gather_context()
    assert "No new context" in second_result


def _make_event_line(event_id: str, source: str = "test") -> str:
    return f'{{"timestamp":"2026-01-01T00:00:00Z","type":"test","event_id":"{event_id}","source":"{source}"}}'


def test_read_tail_lines_returns_last_n_lines(tmp_path: Path) -> None:
    """Verify _read_tail_lines returns only the last N complete lines."""
    module = _load_fresh_context_tool("tail_last_n")
    f = tmp_path / "events.jsonl"
    lines = [_make_event_line(f"e{i}") for i in range(20)]
    f.write_text("\n".join(lines) + "\n")

    result = module._read_tail_lines(f, 5)
    assert len(result) == 5
    for i, line in enumerate(result):
        assert f'"event_id":"e{15 + i}"' in line


def test_read_tail_lines_drops_incomplete_last_line(tmp_path: Path) -> None:
    """Verify _read_tail_lines drops the last line when it lacks a trailing newline."""
    module = _load_fresh_context_tool("tail_incomplete")
    f = tmp_path / "events.jsonl"
    complete = _make_event_line("complete")
    incomplete = '{"partial":"data_no_newline'
    f.write_text(complete + "\n" + incomplete)

    result = module._read_tail_lines(f, 5)
    assert len(result) == 1
    assert "complete" in result[0]
    assert "partial" not in result[0]


def test_read_tail_lines_handles_empty_file(tmp_path: Path) -> None:
    """Verify _read_tail_lines returns empty list for an empty file."""
    module = _load_fresh_context_tool("tail_empty")
    f = tmp_path / "events.jsonl"
    f.write_text("")

    result = module._read_tail_lines(f, 5)
    assert result == []


def test_read_tail_lines_handles_missing_file(tmp_path: Path) -> None:
    """Verify _read_tail_lines returns empty list for a nonexistent file."""
    module = _load_fresh_context_tool("tail_missing")
    f = tmp_path / "nonexistent.jsonl"

    result = module._read_tail_lines(f, 5)
    assert result == []


def test_read_tail_lines_returns_all_when_fewer_than_n(tmp_path: Path) -> None:
    """Verify _read_tail_lines returns all lines when fewer than N exist."""
    module = _load_fresh_context_tool("tail_fewer")
    f = tmp_path / "events.jsonl"
    lines = [_make_event_line(f"e{i}") for i in range(3)]
    f.write_text("\n".join(lines) + "\n")

    result = module._read_tail_lines(f, 10)
    assert len(result) == 3


def test_read_tail_lines_file_only_incomplete_line(tmp_path: Path) -> None:
    """Verify _read_tail_lines returns empty when file has only an incomplete line."""
    module = _load_fresh_context_tool("tail_only_incomplete")
    f = tmp_path / "events.jsonl"
    f.write_text("partial data no newline")

    result = module._read_tail_lines(f, 5)
    assert result == []


def test_get_new_lines_returns_appended_data(tmp_path: Path) -> None:
    """Verify _get_new_lines returns lines appended after a _read_tail_lines call."""
    module = _load_fresh_context_tool("new_lines_append")
    f = tmp_path / "events.jsonl"
    f.write_text(_make_event_line("e1") + "\n")

    # Prime the offset via _read_tail_lines
    module._read_tail_lines(f, 5)

    # Append new data
    with f.open("a") as fh:
        fh.write(_make_event_line("e2") + "\n")

    result = module._get_new_lines(f)
    assert len(result) == 1
    assert '"event_id":"e2"' in result[0]


def test_get_new_lines_drops_incomplete_appended_line(tmp_path: Path) -> None:
    """Verify _get_new_lines skips an incomplete trailing line."""
    module = _load_fresh_context_tool("new_lines_incomplete")
    f = tmp_path / "events.jsonl"
    f.write_text(_make_event_line("e1") + "\n")

    module._read_tail_lines(f, 5)

    # Append one complete line and one incomplete
    with f.open("a") as fh:
        fh.write(_make_event_line("e2") + "\n")
        fh.write("incomplete")

    result = module._get_new_lines(f)
    assert len(result) == 1
    assert '"event_id":"e2"' in result[0]

    # Now "complete" the incomplete line
    with f.open("a") as fh:
        fh.write("_data\n")

    result2 = module._get_new_lines(f)
    assert len(result2) == 1
    assert "incomplete_data" in result2[0]


def test_get_new_lines_returns_empty_when_no_new_data(tmp_path: Path) -> None:
    """Verify _get_new_lines returns empty when file hasn't changed."""
    module = _load_fresh_context_tool("new_lines_no_change")
    f = tmp_path / "events.jsonl"
    f.write_text(_make_event_line("e1") + "\n")

    module._read_tail_lines(f, 5)

    result = module._get_new_lines(f)
    assert result == []


def _make_message_line(event_id: str, conversation_id: str, role: str = "user", content: str = "hello") -> str:
    return (
        f'{{"timestamp":"2026-01-01T00:00:00Z","type":"message",'
        f'"event_id":"{event_id}","source":"messages",'
        f'"conversation_id":"{conversation_id}","role":"{role}","content":"{content}"}}'
    )


def _make_data_event(event_id: str, source: str, data: str = '{"key":"val"}') -> str:
    return (
        f'{{"timestamp":"2026-01-01T00:00:00Z","type":"trigger",'
        f'"event_id":"{event_id}","source":"{source}","data":{data}}}'
    )


def test_gather_context_first_call_shows_transcript_and_triggers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns transcript and trigger events on first call."""
    module = _load_fresh_context_tool("gc_transcript")

    # Set up transcript
    transcript_dir = tmp_path / "logs" / "claude_transcript"
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "events.jsonl"
    transcript_file.write_text(_make_event_line("t1", "claude_transcript") + "\n")

    # Set up monitor events
    monitor_dir = tmp_path / "events" / "monitor"
    monitor_dir.mkdir(parents=True)
    monitor_file = monitor_dir / "events.jsonl"
    monitor_file.write_text(_make_data_event("m1", "monitor") + "\n")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    result = module.gather_context()
    assert "Inner Monologue" in result
    assert "monitor" in result.lower()


def test_gather_context_first_call_groups_messages_by_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context groups messages by conversation on first call."""
    module = _load_fresh_context_tool("gc_messages")

    msgs_dir = tmp_path / "events" / "messages"
    msgs_dir.mkdir(parents=True)
    msgs_file = msgs_dir / "events.jsonl"
    lines = [
        _make_message_line("m1", "conv-A", "user", "hello A"),
        _make_message_line("m2", "conv-B", "user", "hello B"),
        _make_message_line("m3", "conv-A", "assistant", "reply A"),
    ]
    msgs_file.write_text("\n".join(lines) + "\n")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_CONVERSATION_ID", "my-convo")

    result = module.gather_context()
    assert "conv-A" in result
    assert "conv-B" in result


def test_gather_context_incremental_returns_new_trigger_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns new trigger events on subsequent calls."""
    module = _load_fresh_context_tool("gc_incremental_triggers")

    sched_dir = tmp_path / "events" / "scheduled"
    sched_dir.mkdir(parents=True)
    events_file = sched_dir / "events.jsonl"
    events_file.write_text(_make_event_line("s1", "scheduled") + "\n")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    first = module.gather_context()
    assert "scheduled" in first.lower()

    # Append new event
    with events_file.open("a") as fh:
        fh.write(_make_event_line("s2", "scheduled") + "\n")

    second = module.gather_context()
    assert "New scheduled events" in second
    assert "s2" in second


def test_gather_context_incremental_returns_new_messages_from_other_conversations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns new messages from other conversations incrementally."""
    module = _load_fresh_context_tool("gc_incremental_msgs")

    msgs_dir = tmp_path / "events" / "messages"
    msgs_dir.mkdir(parents=True)
    msgs_file = msgs_dir / "events.jsonl"
    msgs_file.write_text(_make_message_line("m1", "other-conv") + "\n")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_CONVERSATION_ID", "my-convo")

    module.gather_context()

    # Append new message from another conversation
    with msgs_file.open("a") as fh:
        fh.write(_make_message_line("m2", "other-conv", "assistant", "new reply") + "\n")

    second = module.gather_context()
    assert "New messages from other conversations" in second
    assert "new reply" in second


def _get_format_events_fn(module_name: str) -> Any:
    """Get the format_events function from either context_tool or extra_context_tool.

    The function is named _format_events in context_tool and _format_extra_events
    in extra_context_tool (to avoid duplicate tool registration by llm --functions).
    """
    if module_name == "context_tool":
        module = _load_fresh_context_tool("fmt_shared")
        return module._format_events
    else:
        module = _load_fresh_extra_context_tool()
        return module._format_extra_events


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_handles_data_events(module_name: str) -> None:
    """Verify _format_events formats data-bearing events correctly."""
    format_events = _get_format_events_fn(module_name)

    lines = [_make_data_event("d1", "monitor")]
    result = format_events(lines)
    assert "[trigger]" in result
    assert "key" in result


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_handles_plain_events(module_name: str) -> None:
    """Verify _format_events formats events without role/content or data."""
    format_events = _get_format_events_fn(module_name)

    line = '{"timestamp":"2026-01-01T00:00:00Z","type":"heartbeat","event_id":"h1","source":"monitor"}'
    result = format_events([line])
    assert "[heartbeat]" in result


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_handles_malformed_json(module_name: str) -> None:
    """Verify _format_events gracefully handles unparseable JSON lines."""
    format_events = _get_format_events_fn(module_name)

    result = format_events(["not valid json at all"])
    assert "not valid json" in result


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_skips_empty_lines(module_name: str) -> None:
    """Verify _format_events skips empty/whitespace-only lines."""
    format_events = _get_format_events_fn(module_name)

    result = format_events(["", "   ", _make_event_line("e1")])
    assert "e1" in result
    lines = [line for line in result.split("\n") if line.strip()]
    assert len(lines) == 1


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_handles_message_event(module_name: str) -> None:
    """Verify _format_events formats message events correctly."""
    format_events = _get_format_events_fn(module_name)

    line = _make_message_line("m1", "conv-1", "user", "hello world")
    result = format_events([line])
    assert "user" in result
    assert "conv-1" in result
    assert "hello world" in result


def test_gather_context_returns_no_context_when_dir_does_not_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context handles a non-existent agent data directory."""
    module = _load_fresh_context_tool("gc_no_dir")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path / "nonexistent"))

    result = module.gather_context()
    assert "does not exist" in result


def test_gather_context_first_call_returns_no_context_when_all_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns 'No context available' when all source dirs are empty."""
    module = _load_fresh_context_tool("gc_all_empty")
    # Create all the log directories but leave them empty (no events.jsonl files)
    for source in ("messages", "scheduled", "mngr/agents", "stop", "monitor"):
        (tmp_path / "events" / source).mkdir(parents=True)
    (tmp_path / "logs" / "claude_transcript").mkdir(parents=True)

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    result = module.gather_context()
    assert "No context available" in result


def test_gather_context_incremental_new_inner_monologue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns new inner monologue entries on subsequent calls."""
    module = _load_fresh_context_tool("gc_inc_monologue")

    transcript_dir = tmp_path / "logs" / "claude_transcript"
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "events.jsonl"
    transcript_file.write_text(_make_event_line("t1", "claude_transcript") + "\n")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    # First call: consumes existing event
    first = module.gather_context()
    assert "Inner Monologue" in first

    # Append new transcript entry
    with transcript_file.open("a") as fh:
        fh.write(_make_event_line("t2", "claude_transcript") + "\n")

    # Second call: should report new inner monologue
    second = module.gather_context()
    assert "New Inner Monologue" in second
    assert "t2" in second


class _GatherContextMessageEnv:
    """Test environment for gather_context message tests.

    Provides a freshly loaded context_tool module, a messages events.jsonl file path,
    and pre-configured MNGR_AGENT_STATE_DIR and LLM_CONVERSATION_ID env vars.
    """

    def __init__(self, module: Any, msgs_file: Path) -> None:
        self.module = module
        self.msgs_file = msgs_file


@pytest.fixture()
def gather_context_msg_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _GatherContextMessageEnv:
    """Set up a fresh context_tool module with a messages directory and conversation env vars."""
    module = _load_fresh_context_tool("gc_msg_env")

    msgs_dir = tmp_path / "events" / "messages"
    msgs_dir.mkdir(parents=True)
    msgs_file = msgs_dir / "events.jsonl"

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_CONVERSATION_ID", "my-convo")

    return _GatherContextMessageEnv(module=module, msgs_file=msgs_file)


def test_gather_context_first_call_handles_json_decode_error_in_messages(
    gather_context_msg_env: _GatherContextMessageEnv,
) -> None:
    """Verify gather_context skips malformed message lines on first call."""
    env = gather_context_msg_env
    env.msgs_file.write_text("not valid json\n" + _make_message_line("m1", "conv-A", "user", "hello") + "\n")

    result = env.module.gather_context()
    assert "conv-A" in result


def test_gather_context_first_call_filters_own_conversation(
    gather_context_msg_env: _GatherContextMessageEnv,
) -> None:
    """Verify gather_context excludes messages from the current conversation."""
    env = gather_context_msg_env
    env.msgs_file.write_text(
        _make_message_line("m1", "my-convo", "user", "my own message")
        + "\n"
        + _make_message_line("m2", "other-convo", "user", "other message")
        + "\n"
    )

    result = env.module.gather_context()
    assert "other-convo" in result
    assert "my-convo" not in result or "my own message" not in result


def test_gather_context_incremental_handles_json_decode_error_in_new_messages(
    gather_context_msg_env: _GatherContextMessageEnv,
) -> None:
    """Verify gather_context skips malformed lines in incremental message reading."""
    env = gather_context_msg_env
    env.msgs_file.write_text(_make_message_line("m1", "other-conv") + "\n")

    # First call
    env.module.gather_context()

    # Append a malformed line and a valid line
    with env.msgs_file.open("a") as fh:
        fh.write("not valid json\n")
        fh.write(_make_message_line("m2", "other-conv", "assistant", "valid reply") + "\n")

    second = env.module.gather_context()
    assert "valid reply" in second


def test_gather_context_incremental_filters_own_conversation_messages(
    gather_context_msg_env: _GatherContextMessageEnv,
) -> None:
    """Verify gather_context incremental excludes own conversation messages."""
    env = gather_context_msg_env
    env.msgs_file.write_text(_make_message_line("m1", "other-conv") + "\n")

    # First call
    env.module.gather_context()

    # Append messages from own conversation only
    with env.msgs_file.open("a") as fh:
        fh.write(_make_message_line("m2", "my-convo", "user", "own msg") + "\n")

    second = env.module.gather_context()
    assert "No new context" in second


def test_get_new_lines_returns_empty_for_nonexistent_file(tmp_path: Path) -> None:
    """Verify _get_new_lines returns empty for a file that doesn't exist."""
    module = _load_fresh_context_tool("new_lines_nonexistent")
    result = module._get_new_lines(tmp_path / "nonexistent.jsonl")
    assert result == []


def test_get_new_lines_returns_empty_when_only_incomplete_data_appended(
    tmp_path: Path,
) -> None:
    """Verify _get_new_lines returns empty when new data has no newline."""
    module = _load_fresh_context_tool("new_lines_only_incomplete")
    f = tmp_path / "events.jsonl"
    f.write_text(_make_event_line("e1") + "\n")

    # Prime offset
    module._read_tail_lines(f, 5)

    # Append data without a newline
    with f.open("a") as fh:
        fh.write("incomplete no newline")

    result = module._get_new_lines(f)
    assert result == []


# -- Provisioning error path tests --


def test_create_mind_symlinks_skips_when_target_does_not_exist() -> None:
    """Verify symlinks are not created when target file doesn't exist."""
    host = StubHost(
        command_results={
            "test -f": StubCommandResult(success=False),
            "test -d": StubCommandResult(success=False),
        }
    )
    create_mind_symlinks(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    # No symlink commands should have been executed
    assert not any("ln -sf" in c for c in host.executed_commands)
    assert not any("ln -sfn" in c for c in host.executed_commands)


def test_create_mind_symlinks_does_not_touch_claude_dir() -> None:
    """Verify that the .claude/ directory at repo root is not touched (no symlink, no removal)."""
    host = StubHost()
    create_mind_symlinks(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)

    # No rm -rf of .claude/ at repo root (we no longer manage .claude symlink)
    rm_cmds = [c for c in host.executed_commands if "rm -rf" in c and "/test/work/.claude" in c]
    assert len(rm_cmds) == 0


def test_create_mind_symlinks_raises_on_symlink_failure() -> None:
    """Verify RuntimeError when symlink creation fails."""
    host = StubHost(
        command_results={
            "ln -sfn": StubCommandResult(success=False, stderr="permission denied"),
        }
    )
    with pytest.raises(RuntimeError, match="Failed to create symlink"):
        create_mind_symlinks(cast(Any, host), Path("/test/work"), "thinking", _DEFAULT_PROVISIONING)


def test_install_llm_toolchain_raises_on_plugin_install_failure_live_chat() -> None:
    """Verify RuntimeError when llm-live-chat plugin installation fails."""
    host = StubHost(
        command_results={
            "llm install llm-live-chat": StubCommandResult(success=False, stderr="live-chat failed"),
        }
    )
    with pytest.raises(RuntimeError, match="Failed to install llm-live-chat"):
        install_llm_toolchain(cast(Any, host), _DEFAULT_PROVISIONING)


def test_resolve_work_dir_abs_raises_on_failure() -> None:
    """Verify RuntimeError when work_dir resolution fails."""
    host = StubHost(
        command_results={
            "&& pwd": StubCommandResult(success=False, stderr="no such dir"),
        }
    )
    with pytest.raises(RuntimeError, match="Failed to resolve absolute path"):
        resolve_work_dir_abs(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)


def test_provision_llm_tools_uses_correct_mode() -> None:
    """Verify LLM tool files are written with 0644 mode."""
    host = StubHost()
    provision_llm_tools(cast(Any, host), Path("/tmp/mngr-test/agents/agent-123"), _DEFAULT_PROVISIONING)

    for _, _, mode in host.written_files:
        assert mode == "0644"


def test_encode_claude_project_dir_name_simple_path() -> None:
    assert encode_claude_project_dir_name(Path("/tmp/foo")) == "-tmp-foo"


def test_encode_claude_project_dir_name_no_dots_or_slashes() -> None:
    assert encode_claude_project_dir_name(Path("simple")) == "simple"


# -- Extra context tool tests --


def _load_fresh_extra_context_tool() -> Any:
    """Import extra_context_tool as a proper package module and reset its state."""
    importlib.reload(extra_context_tool_module)
    return extra_context_tool_module


def _setup_fake_uv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    stdout: str = "",
) -> None:
    """Set up a fake uv script on PATH that returns the given exit code and stdout.

    This avoids monkeypatch.setattr by using a real subprocess with a controlled
    PATH. The fake uv simply echoes stdout and exits with the given code.
    """
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir(exist_ok=True)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(f"#!/bin/bash\necho '{stdout}'\nexit {exit_code}\n")
    fake_uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _setup_uv_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set PATH so that uv cannot be found, triggering FileNotFoundError in subprocess."""
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty_bin))


def _setup_fake_mngr_binary(
    bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int = 0,
    stdout: str = "",
) -> None:
    """Set up a fake mngr binary at <bin_dir>/mngr.

    Creates a shell script that echoes the given stdout and exits with
    the given code. Sets UV_TOOL_BIN_DIR to point to the bin dir.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_mngr = bin_dir / "mngr"
    fake_mngr.write_text(f"#!/bin/bash\necho '{stdout}'\nexit {exit_code}\n")
    fake_mngr.chmod(0o755)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))


@pytest.fixture()
def extra_context_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Path]:
    """Set up a fresh extra_context_tool module with uv not found and MNGR_AGENT_STATE_DIR set.

    Returns the loaded module and the tmp_path (used as agent data directory).
    """
    module = _load_fresh_extra_context_tool()
    _setup_uv_not_found(tmp_path, monkeypatch)
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    return module, tmp_path


# -- Extra context tool: gather_extra_context with file-reading tests --
# These use a fake uv on PATH (or remove uv from PATH) to avoid monkeypatch.setattr.


def test_extra_context_tool_no_env_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context works when MNGR_AGENT_STATE_DIR is not set."""
    module = _load_fresh_extra_context_tool()
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    _setup_uv_not_found(tmp_path, monkeypatch)

    result = module.gather_extra_context()
    assert "Current Agents" in result
    assert "Unable to retrieve" in result


def test_extra_context_tool_with_transcript(extra_context_env: tuple[Any, Path]) -> None:
    """Verify gather_extra_context reads transcript entries."""
    module, data_dir = extra_context_env

    transcript_dir = data_dir / "logs" / "claude_transcript"
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "events.jsonl"
    lines = [_make_event_line(f"t{i}", "claude_transcript") for i in range(5)]
    transcript_file.write_text("\n".join(lines) + "\n")

    result = module.gather_extra_context()
    assert "Extended Inner Monologue" in result
    assert "5 of 5" in result


def test_extra_context_tool_with_conversations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context reads conversations from the DB."""
    llm_data_dir = tmp_path / "llm_data"
    db_path = llm_data_dir / "logs.db"
    monkeypatch.setenv("LLM_USER_PATH", str(llm_data_dir))

    create_mind_conversations_table_in_test_db(db_path)
    write_conversation_to_db(db_path, "conv-1", model="claude-opus-4.6", created_at="2026-01-01T00:00:00Z")
    write_conversation_to_db(db_path, "conv-2", model="claude-sonnet-4-6", created_at="2026-01-01T00:01:00Z")

    module = _load_fresh_extra_context_tool()
    _setup_uv_not_found(tmp_path, monkeypatch)
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    result = module.gather_extra_context()
    assert "All Conversations" in result
    assert "conv-1" in result
    assert "conv-2" in result


def test_extra_context_tool_with_successful_mngr_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context displays agent list on successful mngr list."""
    module = _load_fresh_extra_context_tool()
    bin_dir = tmp_path / "fake_bin"
    _setup_fake_mngr_binary(
        bin_dir,
        monkeypatch,
        exit_code=0,
        stdout='[{"name":"test-agent","state":"RUNNING"}]',
    )

    result = module.gather_extra_context()
    assert "Current Agents" in result
    assert "test-agent" in result


def test_extra_context_tool_with_failed_mngr_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context handles mngr list failure gracefully."""
    module = _load_fresh_extra_context_tool()
    bin_dir = tmp_path / "fake_bin"
    _setup_fake_mngr_binary(bin_dir, monkeypatch, exit_code=1)

    result = module.gather_extra_context()
    assert "No agents or unable to retrieve" in result


def test_extra_context_tool_with_empty_transcript(
    extra_context_env: tuple[Any, Path],
) -> None:
    """Verify gather_extra_context handles empty transcript file."""
    module, data_dir = extra_context_env

    transcript_dir = data_dir / "logs" / "claude_transcript"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "events.jsonl").write_text("")

    result = module.gather_extra_context()
    assert "Extended Inner Monologue" not in result


def test_extra_context_tool_conversations_empty_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context handles an empty mind_conversations table."""
    llm_data_dir = tmp_path / "llm_data"
    llm_data_dir.mkdir(parents=True)
    db_path = llm_data_dir / "logs.db"
    monkeypatch.setenv("LLM_USER_PATH", str(llm_data_dir))

    create_mind_conversations_table_in_test_db(db_path)

    module = _load_fresh_extra_context_tool()
    _setup_uv_not_found(tmp_path, monkeypatch)
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    result = module.gather_extra_context()
    assert "All Conversations" not in result


def test_extra_context_tool_conversations_no_db(
    extra_context_env: tuple[Any, Path],
) -> None:
    """Verify gather_extra_context works when no llm DB exists."""
    module, data_dir = extra_context_env

    result = module.gather_extra_context()
    assert "All Conversations" not in result


def test_extra_context_tool_transcript_with_many_entries(
    extra_context_env: tuple[Any, Path],
) -> None:
    """Verify gather_extra_context limits transcript to last 50 entries."""
    module, data_dir = extra_context_env

    transcript_dir = data_dir / "logs" / "claude_transcript"
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "events.jsonl"
    lines = [_make_event_line(f"t{i}", "claude_transcript") for i in range(100)]
    transcript_file.write_text("\n".join(lines) + "\n")

    result = module.gather_extra_context()
    assert "last 50 of 100" in result


def test_extra_context_tool_conversations_shows_current_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context shows the current model from the DB."""
    llm_data_dir = tmp_path / "llm_data"
    llm_data_dir.mkdir(parents=True)
    db_path = llm_data_dir / "logs.db"
    monkeypatch.setenv("LLM_USER_PATH", str(llm_data_dir))

    create_mind_conversations_table_in_test_db(db_path)
    write_conversation_to_db(db_path, "conv-1", model="claude-sonnet-4-6", created_at="2026-01-01T00:00:00Z")

    module = _load_fresh_extra_context_tool()
    _setup_uv_not_found(tmp_path, monkeypatch)
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    result = module.gather_extra_context()
    assert "All Conversations" in result
    assert "claude-sonnet-4-6" in result


def test_gather_context_first_call_messages_with_empty_lines(
    gather_context_msg_env: _GatherContextMessageEnv,
) -> None:
    """Verify gather_context skips empty lines when parsing messages on first call."""
    env = gather_context_msg_env
    env.msgs_file.write_text("\n" + _make_message_line("m1", "conv-A", "user", "hello") + "\n" + "\n")

    result = env.module.gather_context()
    assert "conv-A" in result


# -- get_mngr_command tests --


def test_get_mngr_command_returns_binary_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_mngr_command returns the per-agent mngr binary path when it exists."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    mngr_bin = bin_dir / "mngr"
    mngr_bin.write_text("#!/bin/bash\n")
    mngr_bin.chmod(0o755)

    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))

    result = get_mngr_command()
    assert result == [str(mngr_bin)]


def test_get_mngr_command_raises_when_env_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_mngr_command raises MngrNotInstalledError when UV_TOOL_BIN_DIR is unset."""
    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)

    with pytest.raises(MngrNotInstalledError, match="UV_TOOL_BIN_DIR is not set"):
        get_mngr_command()


def test_get_mngr_command_raises_when_binary_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_mngr_command raises MngrNotInstalledError when the binary doesn't exist."""
    bin_dir = tmp_path / "empty_bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))

    with pytest.raises(MngrNotInstalledError, match="Per-agent mngr binary not found"):
        get_mngr_command()


# -- provision_event_exclude_sources tests --


def test_provision_event_exclude_sources_writes_to_new_file() -> None:
    """When no minds.toml exists, creates one with event_exclude_sources."""
    host = StubHost()
    work_dir = Path("/work")

    provision_event_exclude_sources(cast(Any, host), work_dir, ("claude/common_transcript",))

    assert len(host.written_text_files) == 1
    path, content = host.written_text_files[0]
    assert "minds.toml" in str(path)
    assert 'event_exclude_sources = ["claude/common_transcript"]' in content


def test_provision_event_exclude_sources_preserves_existing_settings() -> None:
    """When minds.toml has existing settings, they are preserved."""
    host = StubHost(
        text_file_contents={"minds.toml": '[chat]\nmodel = "claude-sonnet-4-6"\n'},
    )
    work_dir = Path("/work")

    provision_event_exclude_sources(cast(Any, host), work_dir, ("claude/common_transcript",))

    assert len(host.written_text_files) == 1
    _, content = host.written_text_files[0]
    assert "claude-sonnet-4-6" in content
    assert 'event_exclude_sources = ["claude/common_transcript"]' in content


def test_provision_event_exclude_sources_merges_with_existing() -> None:
    """When event_exclude_sources already has entries, the new ones are merged in."""
    host = StubHost(
        text_file_contents={
            "minds.toml": '[watchers]\nevent_exclude_sources = ["other/source"]\n',
        },
    )
    work_dir = Path("/work")

    provision_event_exclude_sources(cast(Any, host), work_dir, ("claude/common_transcript",))

    assert len(host.written_text_files) == 1
    _, content = host.written_text_files[0]
    assert "claude/common_transcript" in content
    assert "other/source" in content


def test_provision_event_exclude_sources_skips_when_already_present() -> None:
    """When the desired source is already excluded, no write happens."""
    host = StubHost(
        text_file_contents={
            "minds.toml": '[watchers]\nevent_exclude_sources = ["claude/common_transcript"]\n',
        },
    )
    work_dir = Path("/work")

    provision_event_exclude_sources(cast(Any, host), work_dir, ("claude/common_transcript",))

    assert len(host.written_text_files) == 0


def test_provision_event_exclude_sources_preserves_comments() -> None:
    """Comments in existing minds.toml are preserved when adding exclude sources."""
    toml_with_comments = '# Main chat config\n[chat]\nmodel = "claude-sonnet-4-6"\n'
    host = StubHost(
        text_file_contents={"minds.toml": toml_with_comments},
    )
    work_dir = Path("/work")

    provision_event_exclude_sources(cast(Any, host), work_dir, ("claude/common_transcript",))

    assert len(host.written_text_files) == 1
    _, content = host.written_text_files[0]
    assert "# Main chat config" in content
    assert "claude/common_transcript" in content
