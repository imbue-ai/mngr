"""Unit tests for the mng-changeling-chat CLI module."""

import pytest
from click.testing import CliRunner

from imbue.mng.errors import UserInputError
from imbue.mng.hosts.host import Host
from imbue.mng_changeling_chat.cli import ChatCliOptions
from imbue.mng_changeling_chat.cli import _resolve_latest_conversation_args
from imbue.mng_changeling_chat.cli import chat
from imbue.mng_changeling_chat.cli import resolve_chat_args
from imbue.mng_changeling_chat.testing import TestAgent
from imbue.mng_changeling_chat.testing import create_conversation_events


def test_chat_command_help_shows_all_options() -> None:
    """Verify that the chat --help output contains all expected options."""
    runner = CliRunner()
    result = runner.invoke(chat, ["--help"])

    assert result.exit_code == 0
    assert "--new" in result.output
    assert "--last" in result.output
    assert "--conversation" in result.output
    assert "--allow-unknown-host" in result.output
    assert "--start" in result.output
    assert "AGENT" in result.output


def test_chat_cli_options_has_all_required_fields() -> None:
    """Verify ChatCliOptions declares all expected field types."""
    annotations = ChatCliOptions.__annotations__
    assert annotations["agent"] == (str | None)
    assert annotations["new"] is bool
    assert annotations["last"] is bool
    assert annotations["conversation"] == (str | None)
    assert annotations["start"] is bool
    assert annotations["allow_unknown_host"] is bool


# =========================================================================
# Tests for resolve_chat_args
# =========================================================================


def _make_opts(
    new: bool = False,
    last: bool = False,
    conversation: str | None = None,
) -> ChatCliOptions:
    """Create a ChatCliOptions with the given chat-specific flags."""
    return ChatCliOptions(
        agent=None,
        new=new,
        last=last,
        conversation=conversation,
        start=True,
        allow_unknown_host=False,
        headless=False,
        output_format="human",
        json_flag=False,
        jsonl_flag=False,
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )


def test_resolve_chat_args_new_flag(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent
    opts = _make_opts(new=True)

    result = resolve_chat_args(opts, agent, host, is_interactive=False)

    assert result == ["--new"]


def test_resolve_chat_args_conversation_flag(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent
    opts = _make_opts(conversation="conv-12345")

    result = resolve_chat_args(opts, agent, host, is_interactive=False)

    assert result == ["--resume", "conv-12345"]


def test_resolve_chat_args_last_flag_with_conversations(
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
                "conversation_id": "conv-latest",
                "model": "claude-opus-4-6",
            },
        ],
    )

    opts = _make_opts(last=True)

    result = resolve_chat_args(opts, agent, host, is_interactive=False)

    assert result == ["--resume", "conv-latest"]


def test_resolve_chat_args_last_flag_without_conversations(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent
    opts = _make_opts(last=True)

    result = resolve_chat_args(opts, agent, host, is_interactive=False)

    assert result == ["--new"]


def test_resolve_chat_args_non_interactive_defaults_to_latest(
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
                "conversation_id": "conv-noninteractive",
                "model": "claude-opus-4-6",
            },
        ],
    )

    opts = _make_opts()

    result = resolve_chat_args(opts, agent, host, is_interactive=False)

    assert result == ["--resume", "conv-noninteractive"]


def test_resolve_chat_args_non_interactive_falls_back_to_new(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent
    opts = _make_opts()

    result = resolve_chat_args(opts, agent, host, is_interactive=False)

    assert result == ["--new"]


def test_resolve_chat_args_rejects_mutually_exclusive_new_and_last(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent
    opts = _make_opts(new=True, last=True)

    with pytest.raises(UserInputError, match="Only one of"):
        resolve_chat_args(opts, agent, host, is_interactive=False)


def test_resolve_chat_args_rejects_mutually_exclusive_new_and_conversation(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent
    opts = _make_opts(new=True, conversation="conv-123")

    with pytest.raises(UserInputError, match="Only one of"):
        resolve_chat_args(opts, agent, host, is_interactive=False)


# =========================================================================
# Tests for _resolve_latest_conversation_args
# =========================================================================


def test_resolve_latest_conversation_args_returns_new_when_no_conversations(
    local_host_and_agent: tuple[Host, TestAgent],
) -> None:
    host, agent = local_host_and_agent

    result = _resolve_latest_conversation_args(agent, host)

    assert result == ["--new"]


def test_resolve_latest_conversation_args_resumes_latest(
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
                "conversation_id": "conv-abc",
                "model": "claude-opus-4-6",
            },
        ],
    )

    result = _resolve_latest_conversation_args(agent, host)

    assert result == ["--resume", "conv-abc"]
