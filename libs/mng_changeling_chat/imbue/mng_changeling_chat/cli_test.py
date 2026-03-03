"""Unit tests for the mng-changeling-chat CLI module."""

from click.testing import CliRunner

from imbue.mng_changeling_chat.cli import ChatCliOptions
from imbue.mng_changeling_chat.cli import ConversationInfo
from imbue.mng_changeling_chat.cli import chat


def test_chat_command_is_registered() -> None:
    """Test that the chat command is properly registered."""
    assert chat is not None
    assert chat.name == "chat"


def test_chat_command_help_shows_options() -> None:
    """Verify that the chat --help output contains expected options."""
    runner = CliRunner()
    result = runner.invoke(chat, ["--help"])

    assert result.exit_code == 0
    assert "--new" in result.output
    assert "--last" in result.output
    assert "--conversation" in result.output
    assert "--allow-unknown-host" in result.output
    assert "--start" in result.output


def test_chat_cli_options_has_all_fields() -> None:
    """Test that ChatCliOptions has all required fields."""
    assert hasattr(ChatCliOptions, "__annotations__")
    annotations = ChatCliOptions.__annotations__
    assert "agent" in annotations
    assert "new" in annotations
    assert "last" in annotations
    assert "conversation" in annotations
    assert "start" in annotations
    assert "allow_unknown_host" in annotations


def test_conversation_info_model() -> None:
    """Verify that ConversationInfo can be created and validated."""
    info = ConversationInfo(
        conversation_id="conv-123-abc",
        model="claude-opus-4-6",
        created_at="2026-03-01T00:00:00Z",
        updated_at="2026-03-01T12:00:00Z",
    )

    assert info.conversation_id == "conv-123-abc"
    assert info.model == "claude-opus-4-6"
    assert info.created_at == "2026-03-01T00:00:00Z"
    assert info.updated_at == "2026-03-01T12:00:00Z"


def test_conversation_info_model_validate() -> None:
    """Verify that ConversationInfo.model_validate works with raw dicts."""
    raw = {
        "conversation_id": "conv-456",
        "model": "claude-sonnet-4-6",
        "created_at": "2026-02-28T10:00:00Z",
        "updated_at": "2026-02-28T15:00:00Z",
    }
    info = ConversationInfo.model_validate(raw)

    assert info.conversation_id == "conv-456"
    assert info.model == "claude-sonnet-4-6"


def test_chat_help_shows_agent_argument() -> None:
    """Verify that the chat help includes the AGENT argument."""
    runner = CliRunner()
    result = runner.invoke(chat, ["--help"])

    assert result.exit_code == 0
    assert "AGENT" in result.output
