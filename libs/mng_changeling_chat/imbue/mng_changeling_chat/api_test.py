"""Unit tests for the mng-changeling-chat API module."""

import shlex
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.hosts.host import Host
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.utils.env_utils import build_source_env_shell_commands
from imbue.mng_changeling_chat.api import ChatCommandError
from imbue.mng_changeling_chat.api import _build_chat_env_vars
from imbue.mng_changeling_chat.api import _build_conversation_db_and_messages_paths
from imbue.mng_changeling_chat.api import _build_remote_chat_script
from imbue.mng_changeling_chat.api import _load_env_file_into_dict
from imbue.mng_changeling_chat.api import get_latest_conversation_id
from imbue.mng_changeling_chat.api import list_conversations_on_agent
from imbue.mng_changeling_chat.testing import TestAgent
from imbue.mng_changeling_chat.testing import create_conversation_events
from imbue.mng_changeling_chat.testing import create_message_events

# =========================================================================
# Tests for _build_chat_env_vars
# =========================================================================


def test_build_chat_env_vars_contains_all_required_keys(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    env_vars = _build_chat_env_vars(agent, host)

    assert env_vars["MNG_HOST_DIR"] == str(host.host_dir)
    assert env_vars["MNG_AGENT_STATE_DIR"] == str(host.host_dir / "agents" / str(agent.id))
    assert env_vars["MNG_AGENT_WORK_DIR"] == str(agent.work_dir)
    assert env_vars["MNG_AGENT_ID"] == str(agent.id)
    assert env_vars["MNG_AGENT_NAME"] == str(agent.name)


# =========================================================================
# Tests for _build_remote_chat_script
# =========================================================================


def test_build_remote_chat_script_uses_shlex_quote_for_env_values(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    script = _build_remote_chat_script(agent, host, ["--new"])

    env_vars = _build_chat_env_vars(agent, host)
    for key, value in env_vars.items():
        assert f"export {key}={shlex.quote(value)}" in script


def test_build_remote_chat_script_execs_chat_sh(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    script = _build_remote_chat_script(agent, host, ["--new"])

    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    expected_chat_path = str(agent_state_dir / "commands" / "chat.sh")
    assert f"exec {shlex.quote(expected_chat_path)} --new" in script


def test_build_remote_chat_script_quotes_conversation_id_with_special_chars(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    """Verify that conversation IDs with special characters are safely quoted."""
    host, agent = local_host_and_agent
    dangerous_id = "conv-123; rm -rf /"

    script = _build_remote_chat_script(agent, host, ["--resume", dangerous_id])

    expected_quoted = shlex.quote(dangerous_id)
    assert expected_quoted in script


# =========================================================================
# Tests for _build_conversation_db_and_messages_paths
# =========================================================================


def test_build_conversation_db_and_messages_paths(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    db_path, msg_path = _build_conversation_db_and_messages_paths(agent, host)

    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    assert db_path == agent_state_dir / "llm_data" / "logs.db"
    assert msg_path == agent_state_dir / "events" / "messages" / "events.jsonl"


# =========================================================================
# Tests for list_conversations_on_agent
# =========================================================================


def test_list_conversations_returns_empty_when_no_event_files(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    result = list_conversations_on_agent(agent, host)

    assert result == []


def test_list_conversations_returns_conversations_sorted_by_updated_at(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    create_conversation_events(
        host,
        agent,
        [
            {
                "timestamp": "2026-03-01T10:00:00Z",
                "type": "conversation_created",
                "conversation_id": "conv-older",
                "model": "claude-opus-4.6",
            },
            {
                "timestamp": "2026-03-01T12:00:00Z",
                "type": "conversation_created",
                "conversation_id": "conv-newer",
                "model": "claude-sonnet-4-6",
            },
        ],
    )

    result = list_conversations_on_agent(agent, host)

    assert len(result) == 2
    assert result[0].conversation_id == "conv-newer"
    assert result[1].conversation_id == "conv-older"
    assert result[0].model == "claude-sonnet-4-6"
    assert result[1].model == "claude-opus-4.6"


def test_list_conversations_returns_name_from_tags(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    create_conversation_events(
        host,
        agent,
        [
            {
                "timestamp": "2026-03-01T10:00:00Z",
                "conversation_id": "conv-named",
                "model": "claude-opus-4.6",
                "tags": {"name": "My Chat"},
            },
            {
                "timestamp": "2026-03-01T11:00:00Z",
                "conversation_id": "conv-unnamed",
                "model": "claude-sonnet-4-6",
            },
        ],
    )

    result = list_conversations_on_agent(agent, host)

    assert len(result) == 2
    named = next(c for c in result if c.conversation_id == "conv-named")
    unnamed = next(c for c in result if c.conversation_id == "conv-unnamed")
    assert named.name == "My Chat"
    assert unnamed.name == ""


def test_list_conversations_uses_message_timestamps_for_updated_at(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    create_conversation_events(
        host,
        agent,
        [
            {
                "timestamp": "2026-03-01T10:00:00Z",
                "type": "conversation_created",
                "conversation_id": "conv-old-but-active",
                "model": "claude-opus-4.6",
            },
            {
                "timestamp": "2026-03-01T12:00:00Z",
                "type": "conversation_created",
                "conversation_id": "conv-newer-but-stale",
                "model": "claude-opus-4.6",
            },
        ],
    )

    create_message_events(
        host,
        agent,
        [
            {
                "timestamp": "2026-03-02T15:00:00Z",
                "conversation_id": "conv-old-but-active",
                "role": "user",
                "content": "hello",
            },
        ],
    )

    result = list_conversations_on_agent(agent, host)

    assert len(result) == 2
    assert result[0].conversation_id == "conv-old-but-active"
    assert result[0].updated_at == "2026-03-02T15:00:00Z"


def test_list_conversations_raises_on_command_failure(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    bad_agent = TestAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName("bad-agent"),
        agent_type=AgentTypeName("test"),
        work_dir=Path("/nonexistent/path/that/does/not/exist"),
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=agent.mng_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )

    with pytest.raises(ChatCommandError, match="Failed to list conversations"):
        list_conversations_on_agent(bad_agent, host)


# =========================================================================
# Tests for get_latest_conversation_id
# =========================================================================


def test_get_latest_conversation_id_returns_most_recent(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    create_conversation_events(
        host,
        agent,
        [
            {
                "timestamp": "2026-03-01T10:00:00Z",
                "type": "conversation_created",
                "conversation_id": "conv-aaa",
                "model": "claude-opus-4.6",
            },
            {
                "timestamp": "2026-03-01T12:00:00Z",
                "type": "conversation_created",
                "conversation_id": "conv-bbb",
                "model": "claude-opus-4.6",
            },
        ],
    )

    result = get_latest_conversation_id(agent, host)

    assert result == "conv-bbb"


def test_get_latest_conversation_id_returns_none_when_no_conversations(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    result = get_latest_conversation_id(agent, host)

    assert result is None


# =========================================================================
# Tests for _load_env_file_into_dict
# =========================================================================


def test_load_env_file_parses_key_value_pairs(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-123\nMODEL=opus\n")

    env: dict[str, str] = {}
    _load_env_file_into_dict(env_file, env)

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-123"
    assert env["MODEL"] == "opus"


def test_load_env_file_strips_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("KEY1=\"double-quoted\"\nKEY2='single-quoted'\n")

    env: dict[str, str] = {}
    _load_env_file_into_dict(env_file, env)

    assert env["KEY1"] == "double-quoted"
    assert env["KEY2"] == "single-quoted"


def test_load_env_file_skips_comments_and_blanks(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("# comment\n\nKEY=value\n  # another comment\n")

    env: dict[str, str] = {}
    _load_env_file_into_dict(env_file, env)

    assert env == {"KEY": "value"}


def test_load_env_file_does_nothing_when_missing(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    _load_env_file_into_dict(tmp_path / "nonexistent", env)

    assert env == {}


# =========================================================================
# Tests for _build_source_env_prefix
# =========================================================================


def test_build_source_env_shell_commands_sources_host_then_agent_env() -> None:
    commands = build_source_env_shell_commands(Path("/host/env"), Path("/agent/env"))

    joined = " ".join(commands)
    assert "set -a" in joined
    assert "set +a" in joined
    # Host env sourced before agent env
    host_pos = joined.index("/host/env")
    agent_pos = joined.index("/agent/env")
    assert host_pos < agent_pos


# =========================================================================
# Tests for _build_remote_chat_script (env sourcing)
# =========================================================================


def test_build_remote_chat_script_sources_env_files(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    script = _build_remote_chat_script(agent, host, ["--new"])

    # Should source env files before setting MNG_ vars
    assert "set -a" in script
    assert str(host.host_dir / "env") in script
    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    assert str(agent_state_dir / "env") in script
