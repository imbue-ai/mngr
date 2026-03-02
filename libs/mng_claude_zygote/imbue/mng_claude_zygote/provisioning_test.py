"""Unit tests for the mng_claude_zygote provisioning module."""

import os
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.mng_claude_zygote.conftest import StubCommandResult
from imbue.mng_claude_zygote.conftest import StubHost
from imbue.mng_claude_zygote.data_types import ProvisioningSettings
from imbue.mng_claude_zygote.provisioning import TalkingRoleConstraintError
from imbue.mng_claude_zygote.provisioning import _LLM_TOOL_FILES
from imbue.mng_claude_zygote.provisioning import _SCRIPT_FILES
from imbue.mng_claude_zygote.provisioning import _is_recursive_plugin_registered
from imbue.mng_claude_zygote.provisioning import compute_claude_project_dir_name
from imbue.mng_claude_zygote.provisioning import create_changeling_symlinks
from imbue.mng_claude_zygote.provisioning import create_event_log_directories
from imbue.mng_claude_zygote.provisioning import install_llm_toolchain
from imbue.mng_claude_zygote.provisioning import link_memory_directory
from imbue.mng_claude_zygote.provisioning import load_zygote_resource
from imbue.mng_claude_zygote.provisioning import provision_changeling_scripts
from imbue.mng_claude_zygote.provisioning import provision_default_content
from imbue.mng_claude_zygote.provisioning import provision_llm_tools
from imbue.mng_claude_zygote.provisioning import validate_talking_role_constraints
from imbue.mng_claude_zygote.provisioning import warn_if_mng_unavailable

_DEFAULT_PROVISIONING = ProvisioningSettings()


# -- Resource loading tests --


def test_load_zygote_resource_loads_chat_script() -> None:
    content = load_zygote_resource("chat.sh")
    assert "#!/bin/bash" in content
    assert "chat" in content.lower()


def test_load_zygote_resource_loads_conversation_watcher() -> None:
    content = load_zygote_resource("conversation_watcher.sh")
    assert "#!/bin/bash" in content
    assert "conversation" in content.lower()


def test_load_zygote_resource_loads_event_watcher() -> None:
    content = load_zygote_resource("event_watcher.sh")
    assert "#!/bin/bash" in content
    assert "event" in content.lower()


def test_all_declared_script_files_are_loadable() -> None:
    for script_name in _SCRIPT_FILES:
        content = load_zygote_resource(script_name)
        assert content, f"{script_name} is empty"
        assert content.startswith("#!"), f"{script_name} missing shebang"


def test_all_declared_llm_tool_files_are_loadable() -> None:
    for tool_file in _LLM_TOOL_FILES:
        content = load_zygote_resource(tool_file)
        assert content, f"{tool_file} is empty"
        assert "def " in content, f"{tool_file} missing function definition"


# -- Chat script content tests --


def test_chat_script_supports_new_flag() -> None:
    content = load_zygote_resource("chat.sh")
    assert "--new" in content


def test_chat_script_supports_resume_flag() -> None:
    content = load_zygote_resource("chat.sh")
    assert "--resume" in content


def test_chat_script_supports_as_agent_flag() -> None:
    content = load_zygote_resource("chat.sh")
    assert "--as-agent" in content


def test_chat_script_invokes_llm_live_chat() -> None:
    content = load_zygote_resource("chat.sh")
    assert "llm live-chat" in content


def test_chat_script_invokes_llm_inject() -> None:
    content = load_zygote_resource("chat.sh")
    assert "llm inject" in content


def test_chat_script_writes_conversations_jsonl() -> None:
    content = load_zygote_resource("chat.sh")
    assert "conversations/events.jsonl" in content


def test_chat_script_uses_mng_agent_state_dir() -> None:
    content = load_zygote_resource("chat.sh")
    assert "MNG_AGENT_STATE_DIR" in content


def test_chat_script_passes_llm_tool_functions() -> None:
    content = load_zygote_resource("chat.sh")
    assert "--functions" in content
    assert "llm_tools" in content


def test_chat_script_supports_list_flag() -> None:
    content = load_zygote_resource("chat.sh")
    assert "--list" in content


def test_chat_script_supports_help_flag() -> None:
    content = load_zygote_resource("chat.sh")
    assert "--help" in content


def test_chat_script_uses_jq_not_python_for_json_parsing() -> None:
    """Verify resume uses jq instead of python for single-value JSON extraction."""
    content = load_zygote_resource("chat.sh")
    # resume_conversation should use jq
    assert "jq -r" in content


def test_chat_script_uses_nanosecond_timestamps() -> None:
    """Verify timestamps include nanosecond precision."""
    content = load_zygote_resource("chat.sh")
    assert "%N" in content


def test_chat_script_reports_malformed_lines() -> None:
    """Verify list_conversations reports malformed lines instead of silently skipping."""
    content = load_zygote_resource("chat.sh")
    assert "WARNING" in content or "malformed" in content


def test_chat_script_logs_to_file() -> None:
    """Verify chat.sh writes debug output to a log file."""
    content = load_zygote_resource("chat.sh")
    assert "LOG_FILE" in content
    assert "chat.log" in content


# -- Conversation watcher content tests --


def test_conversation_watcher_queries_sqlite() -> None:
    content = load_zygote_resource("conversation_watcher.sh")
    assert "sqlite3" in content


def test_conversation_watcher_writes_to_messages_events() -> None:
    content = load_zygote_resource("conversation_watcher.sh")
    assert "messages/events.jsonl" in content


def test_conversation_watcher_logs_to_file() -> None:
    """Verify conversation_watcher.sh writes debug output to a log file."""
    content = load_zygote_resource("conversation_watcher.sh")
    assert "LOG_FILE" in content
    assert "conversation_watcher.log" in content


def test_conversation_watcher_logs_sqlite_errors() -> None:
    """Verify conversation_watcher.sh captures and logs sqlite3 errors."""
    content = load_zygote_resource("conversation_watcher.sh")
    assert "WARNING" in content


def test_conversation_watcher_supports_inotifywait() -> None:
    content = load_zygote_resource("conversation_watcher.sh")
    assert "inotifywait" in content


def test_conversation_watcher_uses_id_based_dedup() -> None:
    """Verify conversation_watcher.sh deduplicates events by event_id."""
    content = load_zygote_resource("conversation_watcher.sh")
    assert "file_event_ids" in content
    assert "event_id" in content


def test_conversation_watcher_uses_adaptive_window() -> None:
    """Verify conversation_watcher.sh uses an adaptive window for batch sync."""
    content = load_zygote_resource("conversation_watcher.sh")
    assert "window" in content
    assert "window *= 2" in content


# -- Event watcher content tests --


def test_event_watcher_sends_mng_message() -> None:
    content = load_zygote_resource("event_watcher.sh")
    assert "mng message" in content


def test_event_watcher_watches_messages_events() -> None:
    content = load_zygote_resource("event_watcher.sh")
    assert "messages/events.jsonl" in content


def test_event_watcher_watches_scheduled_events() -> None:
    content = load_zygote_resource("event_watcher.sh")
    assert "scheduled" in content


def test_event_watcher_watches_mng_agents_events() -> None:
    content = load_zygote_resource("event_watcher.sh")
    assert "mng_agents" in content


def test_event_watcher_watches_stop_events() -> None:
    content = load_zygote_resource("event_watcher.sh")
    assert "stop" in content


def test_event_watcher_tracks_offsets() -> None:
    content = load_zygote_resource("event_watcher.sh")
    assert "offset" in content.lower()


def test_event_watcher_supports_inotifywait() -> None:
    content = load_zygote_resource("event_watcher.sh")
    assert "inotifywait" in content


def test_event_watcher_logs_to_file() -> None:
    """Verify event_watcher.sh writes debug output to a log file."""
    content = load_zygote_resource("event_watcher.sh")
    assert "LOG_FILE" in content
    assert "event_watcher.log" in content


def test_event_watcher_logs_send_errors() -> None:
    """Verify event_watcher.sh captures and logs mng message errors."""
    content = load_zygote_resource("event_watcher.sh")
    assert "send_stderr" in content or "ERROR" in content


# -- LLM tool content tests --


def test_context_tool_defines_gather_context() -> None:
    content = load_zygote_resource("context_tool.py")
    assert "def gather_context" in content


def test_context_tool_has_docstring_for_llm() -> None:
    content = load_zygote_resource("context_tool.py")
    assert '"""' in content


def test_context_tool_has_return_type_annotation() -> None:
    content = load_zygote_resource("context_tool.py")
    assert "-> str" in content


def test_extra_context_tool_defines_gather_extra_context() -> None:
    content = load_zygote_resource("extra_context_tool.py")
    assert "def gather_extra_context" in content


def test_extra_context_tool_calls_mng_list() -> None:
    content = load_zygote_resource("extra_context_tool.py")
    assert "mng" in content
    assert "list" in content


# -- Memory linker content tests --


def test_compute_claude_project_dir_name_replaces_slashes() -> None:
    assert compute_claude_project_dir_name("/home/user/project") == "-home-user-project"


def test_compute_claude_project_dir_name_replaces_dots() -> None:
    assert compute_claude_project_dir_name("/home/user/.changelings/agent") == "-home-user--changelings-agent"


def test_link_memory_directory_creates_memory_dir() -> None:
    host = StubHost()
    link_memory_directory(cast(Any, host), Path("/home/user/.changelings/agent"), _DEFAULT_PROVISIONING)

    assert any("mkdir" in c and "/memory" in c for c in host.executed_commands)


def test_link_memory_directory_creates_claude_project_dir_with_home_var() -> None:
    host = StubHost()
    link_memory_directory(cast(Any, host), Path("/home/user/.changelings/agent"), _DEFAULT_PROVISIONING)

    # Must use $HOME (not ~) so tilde expansion works inside quotes
    mkdir_cmds = [c for c in host.executed_commands if "mkdir" in c and ".claude/projects" in c]
    assert len(mkdir_cmds) == 1
    assert "$HOME" in mkdir_cmds[0]
    assert "-home-user--changelings-agent" in mkdir_cmds[0]


def test_link_memory_directory_creates_symlink_with_correct_paths() -> None:
    host = StubHost()
    link_memory_directory(cast(Any, host), Path("/home/user/.changelings/agent"), _DEFAULT_PROVISIONING)

    ln_cmds = [c for c in host.executed_commands if "ln -sfn" in c]
    assert len(ln_cmds) == 1
    # Symlink target should be the memory dir
    assert "/memory" in ln_cmds[0]
    # Symlink source should use $HOME for the Claude project dir
    assert "$HOME/.claude/projects/" in ln_cmds[0]
    assert "-home-user--changelings-agent" in ln_cmds[0]


def test_link_memory_directory_does_not_use_literal_tilde() -> None:
    """Verify that ~ is never used in paths (it doesn't expand inside single quotes)."""
    host = StubHost()
    link_memory_directory(cast(Any, host), Path("/home/user/project"), _DEFAULT_PROVISIONING)

    for cmd in host.executed_commands:
        if ".claude/projects" in cmd:
            assert "~" not in cmd, f"Found literal ~ in command (won't expand in quotes): {cmd}"


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


def test_create_changeling_symlinks_checks_global_md() -> None:
    host = StubHost()
    create_changeling_symlinks(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert any("GLOBAL.md" in c for c in host.executed_commands)


def test_create_changeling_symlinks_checks_thinking_prompt() -> None:
    host = StubHost()
    create_changeling_symlinks(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert any("thinking/PROMPT.md" in c for c in host.executed_commands)


def test_create_changeling_symlinks_checks_thinking_settings() -> None:
    host = StubHost()
    create_changeling_symlinks(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert any("thinking/settings.json" in c for c in host.executed_commands)


def test_create_changeling_symlinks_creates_claude_md() -> None:
    host = StubHost()
    create_changeling_symlinks(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert any("ln -sf" in c and "CLAUDE.md" in c for c in host.executed_commands)


def test_create_changeling_symlinks_creates_claude_local_md() -> None:
    host = StubHost()
    create_changeling_symlinks(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert any("ln -sf" in c and "CLAUDE.local.md" in c for c in host.executed_commands)


def test_create_changeling_symlinks_creates_skills_symlink() -> None:
    host = StubHost()
    create_changeling_symlinks(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert any("ln -sfn" in c and "skills" in c for c in host.executed_commands)


def test_provision_default_content_writes_global_md() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(p) for p, _ in host.written_text_files]
    assert any("GLOBAL.md" in p for p in written_paths)


def test_provision_default_content_writes_thinking_prompt() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(p) for p, _ in host.written_text_files]
    assert any("thinking/PROMPT.md" in p for p in written_paths)


def test_provision_default_content_writes_thinking_settings() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(p) for p, _ in host.written_text_files]
    assert any("thinking/settings.json" in p for p in written_paths)


def test_provision_default_content_writes_skills_to_thinking() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(p) for p, _ in host.written_text_files]
    assert any("thinking/skills/send-message-to-user/SKILL.md" in p for p in written_paths)


def test_provision_changeling_scripts_creates_commands_dir() -> None:
    host = StubHost()
    provision_changeling_scripts(cast(Any, host), _DEFAULT_PROVISIONING)

    assert any("mkdir" in c and "commands" in c for c in host.executed_commands)


def test_provision_changeling_scripts_writes_all_scripts() -> None:
    host = StubHost()
    provision_changeling_scripts(cast(Any, host), _DEFAULT_PROVISIONING)

    written_names = [str(path) for path, _, _ in host.written_files]
    for script_name in _SCRIPT_FILES:
        assert any(script_name in name for name in written_names), f"{script_name} not written"


def test_provision_changeling_scripts_uses_executable_mode() -> None:
    host = StubHost()
    provision_changeling_scripts(cast(Any, host), _DEFAULT_PROVISIONING)

    for _, _, mode in host.written_files:
        assert mode == "0755"


def test_provision_llm_tools_creates_tools_dir() -> None:
    host = StubHost()
    provision_llm_tools(cast(Any, host), _DEFAULT_PROVISIONING)

    assert any("mkdir" in c and "llm_tools" in c for c in host.executed_commands)


def test_provision_llm_tools_writes_all_tool_files() -> None:
    host = StubHost()
    provision_llm_tools(cast(Any, host), _DEFAULT_PROVISIONING)

    written_names = [str(path) for path, _, _ in host.written_files]
    for tool_file in _LLM_TOOL_FILES:
        assert any(tool_file in name for name in written_names), f"{tool_file} not written"


def test_create_event_log_directories_creates_all_source_dirs() -> None:
    host = StubHost()
    create_event_log_directories(cast(Any, host), Path("/tmp/mng-test/agents/agent-123"), _DEFAULT_PROVISIONING)

    for source in ("conversations", "messages", "scheduled", "mng_agents", "stop", "monitor", "claude_transcript"):
        assert any(source in c and "mkdir" in c for c in host.executed_commands), f"Missing mkdir for {source}"


# -- mng availability check tests --


def _make_fake_pm(plugins: list[tuple[str, object]]) -> Any:
    """Create a fake PluginManager that returns the given plugin list."""

    class _FakePM:
        def list_name_plugin(self) -> list[tuple[str, object]]:
            return plugins

    return cast(Any, _FakePM())


def test_warn_if_mng_unavailable_skips_on_local_host() -> None:
    host = StubHost()
    host.is_local = True  # type: ignore[attr-defined]

    warn_if_mng_unavailable(cast(Any, host), _make_fake_pm([]), _DEFAULT_PROVISIONING)

    assert not any("command -v mng" in c for c in host.executed_commands)


def test_warn_if_mng_unavailable_skips_when_recursive_plugin_registered() -> None:
    host = StubHost()
    host.is_local = False  # type: ignore[attr-defined]

    warn_if_mng_unavailable(cast(Any, host), _make_fake_pm([("recursive_mng", object())]), _DEFAULT_PROVISIONING)

    assert not any("command -v mng" in c for c in host.executed_commands)


def test_warn_if_mng_unavailable_checks_on_remote_without_recursive() -> None:
    host = StubHost()
    host.is_local = False  # type: ignore[attr-defined]

    warn_if_mng_unavailable(cast(Any, host), _make_fake_pm([("some_other_plugin", object())]), _DEFAULT_PROVISIONING)

    assert any("command -v mng" in c for c in host.executed_commands)


def test_is_recursive_plugin_registered_returns_true_when_present() -> None:
    assert _is_recursive_plugin_registered(_make_fake_pm([("recursive_mng", object())])) is True


def test_is_recursive_plugin_registered_returns_false_when_absent() -> None:
    assert _is_recursive_plugin_registered(_make_fake_pm([("some_plugin", object())])) is False


# -- context_tool incremental behavior tests --


def _load_fresh_context_tool(name: str) -> Any:
    """Import context_tool as a proper package module and reset its state.

    Uses a real package import (so coverage can track execution) and
    importlib.reload to reinitialize module-level state like _last_file_sizes.
    The ``name`` parameter is accepted for backward-compatibility but unused.
    """
    import importlib

    from imbue.mng_claude_zygote.resources import context_tool

    importlib.reload(context_tool)
    return context_tool


def test_context_tool_gather_context_returns_no_context_when_env_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns a message when MNG_AGENT_STATE_DIR is not set."""
    module = _load_fresh_context_tool("context_tool_test_module")
    monkeypatch.delenv("MNG_AGENT_STATE_DIR", raising=False)

    result = module.gather_context()
    assert "No agent data directory" in result


def test_context_tool_gather_context_returns_no_new_context_on_second_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns incremental results on subsequent calls."""
    # Set up a minimal agent data dir with one scheduled event
    logs_dir = tmp_path / "logs" / "scheduled"
    logs_dir.mkdir(parents=True)
    events_file = logs_dir / "events.jsonl"
    events_file.write_text('{"timestamp":"2026-01-01T00:00:00Z","type":"test","event_id":"e1","source":"scheduled"}\n')

    module = _load_fresh_context_tool("context_tool_incremental_test")
    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))

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


def _make_message_line(event_id: str, cid: str, role: str = "user", content: str = "hello") -> str:
    return (
        f'{{"timestamp":"2026-01-01T00:00:00Z","type":"message",'
        f'"event_id":"{event_id}","source":"messages",'
        f'"conversation_id":"{cid}","role":"{role}","content":"{content}"}}'
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
    monitor_dir = tmp_path / "logs" / "monitor"
    monitor_dir.mkdir(parents=True)
    monitor_file = monitor_dir / "events.jsonl"
    monitor_file.write_text(_make_data_event("m1", "monitor") + "\n")

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))

    result = module.gather_context()
    assert "Inner Monologue" in result
    assert "monitor" in result.lower()


def test_gather_context_first_call_groups_messages_by_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context groups messages by conversation on first call."""
    module = _load_fresh_context_tool("gc_messages")

    msgs_dir = tmp_path / "logs" / "messages"
    msgs_dir.mkdir(parents=True)
    msgs_file = msgs_dir / "events.jsonl"
    lines = [
        _make_message_line("m1", "conv-A", "user", "hello A"),
        _make_message_line("m2", "conv-B", "user", "hello B"),
        _make_message_line("m3", "conv-A", "assistant", "reply A"),
    ]
    msgs_file.write_text("\n".join(lines) + "\n")

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))
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

    sched_dir = tmp_path / "logs" / "scheduled"
    sched_dir.mkdir(parents=True)
    events_file = sched_dir / "events.jsonl"
    events_file.write_text(_make_event_line("s1", "scheduled") + "\n")

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))

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

    msgs_dir = tmp_path / "logs" / "messages"
    msgs_dir.mkdir(parents=True)
    msgs_file = msgs_dir / "events.jsonl"
    msgs_file.write_text(_make_message_line("m1", "other-conv") + "\n")

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_CONVERSATION_ID", "my-convo")

    module.gather_context()

    # Append new message from another conversation
    with msgs_file.open("a") as fh:
        fh.write(_make_message_line("m2", "other-conv", "assistant", "new reply") + "\n")

    second = module.gather_context()
    assert "New messages from other conversations" in second
    assert "new reply" in second


def _load_format_events_module(module_name: str) -> Any:
    """Load the _format_events function from either context_tool or extra_context_tool."""
    if module_name == "context_tool":
        return _load_fresh_context_tool("fmt_shared")
    else:
        return _load_fresh_extra_context_tool()


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_handles_data_events(module_name: str) -> None:
    """Verify _format_events formats data-bearing events correctly."""
    module = _load_format_events_module(module_name)

    lines = [_make_data_event("d1", "monitor")]
    result = module._format_events(lines)
    assert "[trigger]" in result
    assert "key" in result


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_handles_plain_events(module_name: str) -> None:
    """Verify _format_events formats events without role/content or data."""
    module = _load_format_events_module(module_name)

    line = '{"timestamp":"2026-01-01T00:00:00Z","type":"heartbeat","event_id":"h1","source":"monitor"}'
    result = module._format_events([line])
    assert "[heartbeat]" in result


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_handles_malformed_json(module_name: str) -> None:
    """Verify _format_events gracefully handles unparseable JSON lines."""
    module = _load_format_events_module(module_name)

    result = module._format_events(["not valid json at all"])
    assert "not valid json" in result


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_skips_empty_lines(module_name: str) -> None:
    """Verify _format_events skips empty/whitespace-only lines."""
    module = _load_format_events_module(module_name)

    result = module._format_events(["", "   ", _make_event_line("e1")])
    assert "e1" in result
    lines = [line for line in result.split("\n") if line.strip()]
    assert len(lines) == 1


@pytest.mark.parametrize("module_name", ["context_tool", "extra_context_tool"])
def test_format_events_handles_message_event(module_name: str) -> None:
    """Verify _format_events formats message events correctly."""
    module = _load_format_events_module(module_name)

    line = _make_message_line("m1", "conv-1", "user", "hello world")
    result = module._format_events([line])
    assert "user" in result
    assert "conv-1" in result
    assert "hello world" in result


def test_gather_context_returns_no_context_when_dir_does_not_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context handles a non-existent agent data directory."""
    module = _load_fresh_context_tool("gc_no_dir")
    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path / "nonexistent"))

    result = module.gather_context()
    assert "does not exist" in result


def test_gather_context_first_call_returns_no_context_when_all_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_context returns 'No context available' when all source dirs are empty."""
    module = _load_fresh_context_tool("gc_all_empty")
    # Create all the log directories but leave them empty (no events.jsonl files)
    for source in ("claude_transcript", "messages", "scheduled", "mng_agents", "stop", "monitor"):
        (tmp_path / "logs" / source).mkdir(parents=True)

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))

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

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))

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
    and pre-configured MNG_AGENT_STATE_DIR and LLM_CONVERSATION_ID env vars.
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

    msgs_dir = tmp_path / "logs" / "messages"
    msgs_dir.mkdir(parents=True)
    msgs_file = msgs_dir / "events.jsonl"

    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))
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


def test_create_changeling_symlinks_skips_when_target_does_not_exist() -> None:
    """Verify symlinks are not created when target file doesn't exist."""
    host = StubHost(
        command_results={
            "test -f": StubCommandResult(success=False),
            "test -d": StubCommandResult(success=False),
        }
    )
    create_changeling_symlinks(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    # No symlink commands should have been executed
    assert not any("ln -sf" in c for c in host.executed_commands)
    assert not any("ln -sfn" in c for c in host.executed_commands)


def test_create_changeling_symlinks_raises_on_symlink_failure() -> None:
    """Verify RuntimeError when symlink creation fails."""
    host = StubHost(
        command_results={
            "ln -sf": StubCommandResult(success=False, stderr="permission denied"),
        }
    )
    with pytest.raises(RuntimeError, match="Failed to create symlink"):
        create_changeling_symlinks(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)


def test_install_llm_toolchain_raises_on_plugin_install_failure_live_chat() -> None:
    """Verify RuntimeError when llm-live-chat plugin installation fails."""
    host = StubHost(
        command_results={
            "llm install llm-live-chat": StubCommandResult(success=False, stderr="live-chat failed"),
        }
    )
    with pytest.raises(RuntimeError, match="Failed to install llm-live-chat"):
        install_llm_toolchain(cast(Any, host), _DEFAULT_PROVISIONING)


def test_link_memory_directory_raises_on_resolve_failure() -> None:
    """Verify RuntimeError when work_dir resolution fails."""
    host = StubHost(
        command_results={
            "&& pwd": StubCommandResult(success=False, stderr="no such dir"),
        }
    )
    with pytest.raises(RuntimeError, match="Failed to resolve absolute path"):
        link_memory_directory(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)


def test_link_memory_directory_raises_on_link_failure() -> None:
    """Verify RuntimeError when memory symlink creation fails."""
    host = StubHost(
        command_results={
            "ln -sfn": StubCommandResult(success=False, stderr="link failed"),
        }
    )
    with pytest.raises(RuntimeError, match="Failed to link memory directory"):
        link_memory_directory(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)


def test_provision_llm_tools_uses_correct_mode() -> None:
    """Verify LLM tool files are written with 0644 mode."""
    host = StubHost()
    provision_llm_tools(cast(Any, host), _DEFAULT_PROVISIONING)

    for _, _, mode in host.written_files:
        assert mode == "0644"


def test_compute_claude_project_dir_name_simple_path() -> None:
    assert compute_claude_project_dir_name("/tmp/foo") == "-tmp-foo"


def test_compute_claude_project_dir_name_no_dots_or_slashes() -> None:
    assert compute_claude_project_dir_name("simple") == "simple"


# -- Extra context tool tests --


def _load_fresh_extra_context_tool() -> Any:
    """Import extra_context_tool as a proper package module and reset its state."""
    import importlib

    from imbue.mng_claude_zygote.resources import extra_context_tool

    importlib.reload(extra_context_tool)
    return extra_context_tool


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


@pytest.fixture()
def extra_context_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Path]:
    """Set up a fresh extra_context_tool module with uv not found and MNG_AGENT_STATE_DIR set.

    Returns the loaded module and the tmp_path (used as agent data directory).
    """
    module = _load_fresh_extra_context_tool()
    _setup_uv_not_found(tmp_path, monkeypatch)
    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(tmp_path))
    return module, tmp_path


# -- Extra context tool: gather_extra_context with file-reading tests --
# These use a fake uv on PATH (or remove uv from PATH) to avoid monkeypatch.setattr.


def test_extra_context_tool_no_env_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context works when MNG_AGENT_STATE_DIR is not set."""
    module = _load_fresh_extra_context_tool()
    monkeypatch.delenv("MNG_AGENT_STATE_DIR", raising=False)
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


def test_extra_context_tool_with_conversations(extra_context_env: tuple[Any, Path]) -> None:
    """Verify gather_extra_context reads conversation events."""
    module, data_dir = extra_context_env

    conv_dir = data_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    conv_file = conv_dir / "events.jsonl"
    conv_file.write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","type":"conversation_created","event_id":"c1",'
        '"source":"conversations","conversation_id":"conv-1","model":"claude-opus-4-6"}\n'
        '{"timestamp":"2026-01-01T00:01:00Z","type":"conversation_created","event_id":"c2",'
        '"source":"conversations","conversation_id":"conv-2","model":"claude-sonnet-4-6"}\n'
    )

    result = module.gather_extra_context()
    assert "All Conversations" in result
    assert "conv-1" in result
    assert "conv-2" in result


def test_extra_context_tool_with_successful_mng_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context displays agent list on successful uv run mng list."""
    module = _load_fresh_extra_context_tool()
    _setup_fake_uv(tmp_path, monkeypatch, exit_code=0, stdout='[{"name":"test-agent","state":"RUNNING"}]')
    monkeypatch.delenv("MNG_AGENT_STATE_DIR", raising=False)

    result = module.gather_extra_context()
    assert "Current Agents" in result
    assert "test-agent" in result


def test_extra_context_tool_with_failed_mng_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify gather_extra_context handles mng list failure gracefully."""
    module = _load_fresh_extra_context_tool()
    _setup_fake_uv(tmp_path, monkeypatch, exit_code=1)
    monkeypatch.delenv("MNG_AGENT_STATE_DIR", raising=False)

    result = module.gather_extra_context()
    assert "No agents or unable to retrieve" in result


def test_extra_context_tool_with_empty_transcript(extra_context_env: tuple[Any, Path]) -> None:
    """Verify gather_extra_context handles empty transcript file."""
    module, data_dir = extra_context_env

    transcript_dir = data_dir / "logs" / "claude_transcript"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "events.jsonl").write_text("")

    result = module.gather_extra_context()
    assert "Extended Inner Monologue" not in result


def test_extra_context_tool_conversations_with_malformed_json(extra_context_env: tuple[Any, Path]) -> None:
    """Verify gather_extra_context skips malformed conversation lines."""
    module, data_dir = extra_context_env

    conv_dir = data_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    conv_file = conv_dir / "events.jsonl"
    conv_file.write_text(
        "not valid json\n"
        '{"timestamp":"2026-01-01T00:00:00Z","type":"conversation_created","event_id":"c1",'
        '"source":"conversations","conversation_id":"conv-1","model":"claude-opus-4-6"}\n'
    )

    result = module.gather_extra_context()
    assert "conv-1" in result


def test_extra_context_tool_conversations_missing_key(extra_context_env: tuple[Any, Path]) -> None:
    """Verify gather_extra_context skips conversations lines missing conversation_id."""
    module, data_dir = extra_context_env

    conv_dir = data_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    conv_file = conv_dir / "events.jsonl"
    conv_file.write_text('{"timestamp":"2026-01-01T00:00:00Z","type":"test","event_id":"c1"}\n')

    result = module.gather_extra_context()
    assert "All Conversations" not in result


def test_extra_context_tool_transcript_with_many_entries(extra_context_env: tuple[Any, Path]) -> None:
    """Verify gather_extra_context limits transcript to last 50 entries."""
    module, data_dir = extra_context_env

    transcript_dir = data_dir / "logs" / "claude_transcript"
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "events.jsonl"
    lines = [_make_event_line(f"t{i}", "claude_transcript") for i in range(100)]
    transcript_file.write_text("\n".join(lines) + "\n")

    result = module.gather_extra_context()
    assert "last 50 of 100" in result


def test_extra_context_tool_conversations_updates_existing_conversation(
    extra_context_env: tuple[Any, Path],
) -> None:
    """Verify gather_extra_context uses the latest event for each conversation."""
    module, data_dir = extra_context_env

    conv_dir = data_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    conv_file = conv_dir / "events.jsonl"
    conv_file.write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","type":"conversation_created","event_id":"c1",'
        '"source":"conversations","conversation_id":"conv-1","model":"claude-opus-4-6"}\n'
        '{"timestamp":"2026-01-01T00:01:00Z","type":"model_changed","event_id":"c2",'
        '"source":"conversations","conversation_id":"conv-1","model":"claude-sonnet-4-6"}\n'
    )

    result = module.gather_extra_context()
    assert "All Conversations" in result
    assert "claude-sonnet-4-6" in result


def test_extra_context_tool_conversations_with_empty_lines(extra_context_env: tuple[Any, Path]) -> None:
    """Verify gather_extra_context skips empty lines in conversations file."""
    module, data_dir = extra_context_env

    conv_dir = data_dir / "logs" / "conversations"
    conv_dir.mkdir(parents=True)
    conv_file = conv_dir / "events.jsonl"
    conv_file.write_text(
        "\n"
        '{"timestamp":"2026-01-01T00:00:00Z","type":"conversation_created","event_id":"c1",'
        '"source":"conversations","conversation_id":"conv-1","model":"claude-opus-4-6"}\n'
        "\n"
    )

    result = module.gather_extra_context()
    assert "conv-1" in result


def test_gather_context_first_call_messages_with_empty_lines(
    gather_context_msg_env: _GatherContextMessageEnv,
) -> None:
    """Verify gather_context skips empty lines when parsing messages on first call."""
    env = gather_context_msg_env
    env.msgs_file.write_text("\n" + _make_message_line("m1", "conv-A", "user", "hello") + "\n" + "\n")

    result = env.module.gather_context()
    assert "conv-A" in result


# -- mng availability check tests (warning path) --


def test_warn_if_mng_unavailable_warns_when_missing_on_remote() -> None:
    """Verify warn_if_mng_unavailable checks for mng on remote host when not found."""
    host = StubHost(
        command_results={"command -v mng": StubCommandResult(success=False)},
    )
    host.is_local = False  # type: ignore[attr-defined]

    # Should not raise, just warn
    warn_if_mng_unavailable(cast(Any, host), _make_fake_pm([]), _DEFAULT_PROVISIONING)

    # Verify the mng availability check was executed
    assert any("command -v mng" in c for c in host.executed_commands)


# -- Default content provisioning tests --


def test_all_default_thinking_files_are_loadable() -> None:
    """Verify all declared default thinking dir files can be loaded from resources."""
    from imbue.mng_claude_zygote.provisioning import _DEFAULT_THINKING_DIR_FILES

    for resource_name, _ in _DEFAULT_THINKING_DIR_FILES:
        content = load_zygote_resource(f"defaults/{resource_name}")
        assert content, f"defaults/{resource_name} is empty"


def test_all_default_work_dir_files_are_loadable() -> None:
    """Verify all declared default work dir files can be loaded from resources."""
    from imbue.mng_claude_zygote.provisioning import _DEFAULT_WORK_DIR_FILES

    for resource_name, _ in _DEFAULT_WORK_DIR_FILES:
        content = load_zygote_resource(f"defaults/{resource_name}")
        assert content, f"defaults/{resource_name} is empty"


def test_all_default_skill_files_are_loadable() -> None:
    """Verify all declared default skill files can be loaded from resources."""
    from imbue.mng_claude_zygote.provisioning import _DEFAULT_SKILL_DIRS

    for skill_name in _DEFAULT_SKILL_DIRS:
        content = load_zygote_resource(f"defaults/thinking/skills/{skill_name}/SKILL.md")
        assert content, f"defaults/thinking/skills/{skill_name}/SKILL.md is empty"


def test_default_global_md_describes_agent_role() -> None:
    """Verify the default GLOBAL.md describes the agent's role in the changelings framework."""
    content = load_zygote_resource("defaults/GLOBAL.md")
    assert "changelings" in content


def test_default_thinking_prompt_describes_event_processing() -> None:
    """Verify the default thinking/PROMPT.md describes the event processing role."""
    content = load_zygote_resource("defaults/thinking/PROMPT.md")
    assert "event" in content.lower()
    assert "messages" in content


def test_default_thinking_settings_json_is_valid_json() -> None:
    """Verify the default thinking/settings.json is valid JSON."""
    import json

    content = load_zygote_resource("defaults/thinking/settings.json")
    parsed = json.loads(content)
    assert "permissions" in parsed


def test_default_new_chat_skill_has_frontmatter_and_references_chat_script() -> None:
    """Verify the send-message-to-user skill has YAML frontmatter and references the chat.sh script."""
    content = load_zygote_resource("defaults/thinking/skills/send-message-to-user/SKILL.md")
    assert content.startswith("---")
    assert "name: send-message-to-user" in content
    assert "description:" in content
    assert "chat.sh" in content
    assert "--new" in content
    assert "--as-agent" in content


def test_default_list_conversations_skill_has_frontmatter_and_references_chat_script() -> None:
    """Verify the list-conversations skill has YAML frontmatter and references chat.sh --list."""
    content = load_zygote_resource("defaults/thinking/skills/list-conversations/SKILL.md")
    assert content.startswith("---")
    assert "name: list-conversations" in content
    assert "description:" in content
    assert "chat.sh" in content
    assert "--list" in content


def test_provision_default_content_writes_missing_files() -> None:
    """Verify provision_default_content writes all default files when none exist."""
    from imbue.mng_claude_zygote.provisioning import provision_default_content

    host = StubHost(
        command_results={"test -f": StubCommandResult(success=False)},
    )
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(path) for path, _ in host.written_text_files]
    assert any("GLOBAL.md" in p for p in written_paths)
    assert any("talking/PROMPT.md" in p for p in written_paths)
    assert any("thinking/PROMPT.md" in p for p in written_paths)
    assert any("thinking/settings.json" in p for p in written_paths)
    assert any("thinking/skills/send-message-to-user/SKILL.md" in p for p in written_paths)
    assert any("thinking/skills/list-conversations/SKILL.md" in p for p in written_paths)
    assert any("thinking/skills/delegate-task/SKILL.md" in p for p in written_paths)
    assert any("thinking/skills/list-event-types/SKILL.md" in p for p in written_paths)
    assert any("thinking/skills/get-event-type-info/SKILL.md" in p for p in written_paths)


def test_provision_default_content_skips_existing_files() -> None:
    """Verify provision_default_content does not overwrite existing files."""
    from imbue.mng_claude_zygote.provisioning import provision_default_content

    # test -f returns success (file exists) by default in StubHost
    host = StubHost()
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert len(host.written_text_files) == 0


def test_provision_default_content_creates_parent_directories() -> None:
    """Verify provision_default_content creates parent directories for missing files."""
    from imbue.mng_claude_zygote.provisioning import provision_default_content

    host = StubHost(
        command_results={"test -f": StubCommandResult(success=False)},
    )
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    mkdir_cmds = [c for c in host.executed_commands if "mkdir -p" in c]
    assert len(mkdir_cmds) > 0


def test_provision_default_content_writes_to_thinking_dir() -> None:
    """Verify provision_default_content writes thinking agent files to the thinking directory."""
    from imbue.mng_claude_zygote.provisioning import provision_default_content

    host = StubHost(
        command_results={"test -f": StubCommandResult(success=False)},
    )
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(path) for path, _ in host.written_text_files]
    assert any("thinking/PROMPT.md" in p for p in written_paths)
    assert any("thinking/settings.json" in p for p in written_paths)


# -- validate_talking_role_constraints tests --


def test_validate_talking_role_constraints_passes_when_nothing_exists() -> None:
    """Verify validation passes when talking/ has no skills or settings."""
    host = StubHost(
        command_results={"test -e": StubCommandResult(success=False)},
    )
    validate_talking_role_constraints(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)


def test_validate_talking_role_constraints_raises_for_skills_directory() -> None:
    """Verify validation raises with an actionable message when talking/skills/ exists."""
    host = StubHost(
        command_results={"talking/skills": StubCommandResult(success=True)},
    )
    with pytest.raises(TalkingRoleConstraintError, match="skills") as exc_info:
        validate_talking_role_constraints(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)
    assert "Remove this path" in str(exc_info.value)


def test_validate_talking_role_constraints_raises_for_settings_json() -> None:
    """Verify validation raises when talking/settings.json exists."""
    host = StubHost(
        command_results={
            "talking/skills": StubCommandResult(success=False),
            "talking/settings.json": StubCommandResult(success=True),
        },
    )
    with pytest.raises(TalkingRoleConstraintError, match="settings.json"):
        validate_talking_role_constraints(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)


# -- talking/PROMPT.md default content tests --


def test_default_talking_prompt_describes_voice_role() -> None:
    """Verify the default talking/PROMPT.md describes the talking role."""
    content = load_zygote_resource("defaults/talking/PROMPT.md")
    assert "talking" in content.lower()
    assert "voice" in content.lower() or "reply" in content.lower() or "conversation" in content.lower()


def test_provision_default_content_writes_talking_prompt() -> None:
    """Verify provision_default_content writes talking/PROMPT.md when missing."""
    host = StubHost(
        command_results={"test -f": StubCommandResult(success=False)},
    )
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(path) for path, _ in host.written_text_files]
    assert any("talking/PROMPT.md" in p for p in written_paths)


# -- chat.sh system prompt tests --


def test_chat_script_references_talking_prompt() -> None:
    """Verify chat.sh references the talking/PROMPT.md file."""
    content = load_zygote_resource("chat.sh")
    assert "talking/PROMPT.md" in content
    assert "MNG_AGENT_WORK_DIR" in content


def test_chat_script_passes_system_prompt_to_llm() -> None:
    """Verify chat.sh passes the system prompt via -s flag to llm."""
    content = load_zygote_resource("chat.sh")
    assert "-s " in content or '-s "' in content


# -- skill content quality tests --


def test_delegate_task_skill_has_frontmatter_and_mng_commands() -> None:
    """Verify the delegate-task skill has YAML frontmatter and references mng create."""
    content = load_zygote_resource("defaults/thinking/skills/delegate-task/SKILL.md")
    assert content.startswith("---")
    assert "name: delegate-task" in content
    assert "description:" in content
    assert "mng create" in content


def test_list_event_types_skill_has_frontmatter_and_event_sources() -> None:
    """Verify the list-event-types skill has YAML frontmatter and describes event sources."""
    content = load_zygote_resource("defaults/thinking/skills/list-event-types/SKILL.md")
    assert content.startswith("---")
    assert "name: list-event-types" in content
    assert "messages" in content
    assert "mng_agents" in content
    assert "scheduled" in content
    assert "stop" in content


def test_get_event_type_info_skill_has_frontmatter() -> None:
    """Verify the get-event-type-info skill has YAML frontmatter and content."""
    content = load_zygote_resource("defaults/thinking/skills/get-event-type-info/SKILL.md")
    assert content.startswith("---")
    assert "name: get-event-type-info" in content
    assert len(content) > 100  # must have substantive content


# -- GLOBAL.md content quality tests --


def test_global_md_describes_repo_structure() -> None:
    """Verify the GLOBAL.md describes the repository structure."""
    content = load_zygote_resource("defaults/GLOBAL.md")
    assert "talking/" in content
    assert "thinking/" in content
    assert "working/" in content
    assert "verifying/" in content
    assert "PROMPT.md" in content


def test_global_md_describes_event_system() -> None:
    """Verify the GLOBAL.md describes the event system."""
    content = load_zygote_resource("defaults/GLOBAL.md")
    assert "event" in content.lower()
    assert "messages" in content
    assert "mng_agents" in content


def test_global_md_describes_agent_roles() -> None:
    """Verify the GLOBAL.md describes the different agent roles."""
    content = load_zygote_resource("defaults/GLOBAL.md")
    assert "thinking" in content.lower()
    assert "talking" in content.lower()
    assert "working" in content.lower()
    assert "verifying" in content.lower()
