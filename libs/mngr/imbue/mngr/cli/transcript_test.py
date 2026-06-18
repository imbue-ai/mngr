import json
from pathlib import Path

import pluggy
import tomlkit
from click.testing import CliRunner

from imbue.mngr.cli.testing import SAMPLE_TRANSCRIPT_EVENTS
from imbue.mngr.cli.testing import create_agent_with_events_dir
from imbue.mngr.cli.testing import create_agent_with_sample_transcript
from imbue.mngr.cli.testing import write_common_transcript_events
from imbue.mngr.cli.transcript import TranscriptCliOptions
from imbue.mngr.cli.transcript import _format_event_human
from imbue.mngr.cli.transcript import _get_event_role
from imbue.mngr.cli.transcript import _parse_transcript_events
from imbue.mngr.cli.transcript import transcript
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.toml_config import load_config_file_tomlkit
from imbue.mngr.utils.toml_config import save_config_file

_DEFAULT_TARGET = AgentAddress(agent=AgentName("my-agent"))


def _make_transcript_opts(
    target: AgentOrHostAddress = _DEFAULT_TARGET,
    role: tuple[str, ...] = (),
    tail: int | None = None,
    head: int | None = None,
) -> TranscriptCliOptions:
    return TranscriptCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
        target=target,
        role=role,
        tail=tail,
        head=head,
    )


# =============================================================================
# TranscriptCliOptions tests
# =============================================================================


def test_transcript_cli_options_can_be_constructed() -> None:
    opts = _make_transcript_opts()
    assert opts.target == AgentAddress(agent=AgentName("my-agent"))
    assert opts.role == ()
    assert opts.tail is None
    assert opts.head is None


def test_transcript_cli_options_with_roles() -> None:
    opts = _make_transcript_opts(role=("user", "assistant"))
    assert opts.role == ("user", "assistant")


def test_transcript_cli_options_with_tail() -> None:
    opts = _make_transcript_opts(tail=10)
    assert opts.tail == 10


def test_transcript_cli_options_with_head() -> None:
    opts = _make_transcript_opts(head=5)
    assert opts.head == 5


# =============================================================================
# _get_event_role tests
# =============================================================================


def test_get_event_role_from_explicit_role_field() -> None:
    assert _get_event_role({"role": "user"}) == "user"


def test_get_event_role_from_user_message_type() -> None:
    assert _get_event_role({"type": "user_message"}) == "user"


def test_get_event_role_from_assistant_message_type() -> None:
    assert _get_event_role({"type": "assistant_message"}) == "assistant"


def test_get_event_role_from_tool_result_type() -> None:
    assert _get_event_role({"type": "tool_result"}) == "tool"


def test_get_event_role_returns_none_for_unknown_type() -> None:
    assert _get_event_role({"type": "something_else"}) is None


def test_get_event_role_returns_none_for_empty_event() -> None:
    assert _get_event_role({}) is None


# =============================================================================
# _parse_transcript_events tests
# =============================================================================


def test_parse_transcript_events_parses_jsonl_lines() -> None:
    content = (
        json.dumps({"type": "user_message", "content": "hello"})
        + "\n"
        + json.dumps({"type": "assistant_message", "text": "hi"})
        + "\n"
    )
    events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 2
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "assistant_message"


def test_parse_transcript_events_filters_by_role() -> None:
    content = (
        json.dumps({"type": "user_message", "role": "user", "content": "hello"})
        + "\n"
        + json.dumps({"type": "assistant_message", "role": "assistant", "text": "hi"})
        + "\n"
        + json.dumps({"type": "tool_result", "tool_name": "Bash", "output": "ok"})
        + "\n"
    )
    events = _parse_transcript_events(content, roles=("user",), source_description="test transcript")
    assert len(events) == 1
    assert events[0]["type"] == "user_message"


def test_parse_transcript_events_filters_multiple_roles() -> None:
    content = (
        json.dumps({"type": "user_message", "role": "user", "content": "hello"})
        + "\n"
        + json.dumps({"type": "assistant_message", "role": "assistant", "text": "hi"})
        + "\n"
        + json.dumps({"type": "tool_result", "tool_name": "Bash", "output": "ok"})
        + "\n"
    )
    events = _parse_transcript_events(content, roles=("user", "tool"), source_description="test transcript")
    assert len(events) == 2
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "tool_result"


def test_parse_transcript_events_skips_blank_lines() -> None:
    content = "\n\n" + json.dumps({"type": "user_message", "content": "hello"}) + "\n\n"
    events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 1


def test_parse_transcript_events_skips_malformed_json() -> None:
    content = "not json\n" + json.dumps({"type": "user_message", "content": "hello"}) + "\n"
    # Mid-file malformed lines now emit a logger.warning; absorb it so it doesn't
    # leak to uncaptured output. The dedicated mid-file warning test asserts on it.
    with capture_loguru(level="WARNING"):
        events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 1


def test_parse_transcript_events_warns_on_mid_file_corruption() -> None:
    content = (
        json.dumps({"type": "user_message", "content": "hello"})
        + "\n"
        + "this is not json {{{\n"
        + json.dumps({"type": "assistant_message", "text": "hi"})
        + "\n"
    )
    with capture_loguru(level="WARNING") as log_output:
        events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 2
    assert "Skipped corrupt JSONL line" in log_output.getvalue()


def test_parse_transcript_events_silent_on_partial_last_line() -> None:
    content = json.dumps({"type": "user_message", "content": "hello"}) + "\nincomplete{"
    with capture_loguru(level="WARNING") as log_output:
        events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 1
    assert log_output.getvalue() == ""


# =============================================================================
# _format_event_human tests
# =============================================================================


def test_format_event_human_user_message() -> None:
    event = {
        "type": "user_message",
        "timestamp": "2026-01-01T00:00:00.123Z",
        "content": "Hello world",
    }
    result = _format_event_human(event)
    assert "[2026-01-01T00:00:00Z] user:" in result
    assert "Hello world" in result


def test_format_event_human_assistant_message_with_text() -> None:
    event = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:01.456Z",
        "text": "Here is my response",
        "tool_calls": [],
        "parts": [{"type": "text", "content": "Here is my response"}],
    }
    result = _format_event_human(event)
    assert "[2026-01-01T00:00:01Z] assistant:" in result
    assert "Here is my response" in result


def test_format_event_human_assistant_message_with_tool_calls() -> None:
    event = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:02Z",
        "text": "",
        "tool_calls": [
            {"tool_call_id": "c1", "tool_name": "Read", "input_preview": '{"file":"test.py"}'},
        ],
        "parts": [
            {"type": "tool_call", "tool_call_id": "c1", "tool_name": "Read", "input_preview": '{"file":"test.py"}'},
        ],
    }
    result = _format_event_human(event)
    assert "assistant:" in result
    assert "-> Read(" in result


def test_format_event_human_tool_result() -> None:
    event = {
        "type": "tool_result",
        "timestamp": "2026-01-01T00:00:03Z",
        "tool_name": "Bash",
        "output": "command output here",
        "is_error": False,
    }
    result = _format_event_human(event)
    assert "tool (Bash):" in result
    assert "command output here" in result
    assert "[ERROR]" not in result


def test_format_event_human_tool_result_error() -> None:
    event = {
        "type": "tool_result",
        "timestamp": "2026-01-01T00:00:03Z",
        "tool_name": "Bash",
        "output": "failed",
        "is_error": True,
    }
    result = _format_event_human(event)
    assert "[ERROR]" in result


def test_format_event_human_tool_result_truncates_long_output() -> None:
    event = {
        "type": "tool_result",
        "timestamp": "2026-01-01T00:00:03Z",
        "tool_name": "Read",
        "output": "x" * 1000,
        "is_error": False,
    }
    result = _format_event_human(event)
    assert "..." in result
    # Output should be truncated (500 chars + "...")
    output_line = result.split("\n", 1)[1]
    assert len(output_line) <= 504


def test_format_event_human_assistant_no_content() -> None:
    event = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:00Z",
        "text": "",
        "tool_calls": [],
    }
    result = _format_event_human(event)
    assert "(no content)" in result


# =============================================================================
# CLI validation tests
# =============================================================================


def test_transcript_cli_rejects_head_and_tail_together(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(
        transcript,
        ["my-agent", "--head", "5", "--tail", "10"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot specify both --head and --tail" in result.output


# =============================================================================
# Integration tests with real agent data
# =============================================================================


def test_transcript_cli_reads_and_displays_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-human-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-human-test"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "Hello" in result.output
    assert "World" in result.output
    assert "user:" in result.output
    assert "assistant:" in result.output


def test_transcript_cli_reads_jsonl_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-jsonl-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-jsonl-test", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 3
    parsed = json.loads(lines[0])
    assert parsed["type"] == "user_message"
    assert parsed["content"] == "Hello"


def test_transcript_cli_reads_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-json-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-json-test", "--format", "json"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert len(parsed) == 3


def test_transcript_cli_filters_by_role(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-role-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-role-test", "--role", "user", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "user_message"


def test_transcript_cli_filters_by_multiple_roles(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-multirole-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-multirole-test", "--role", "user", "--role", "assistant", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2


def test_transcript_cli_applies_tail(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    numbered_events = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "user_message",
            "event_id": f"e{i}",
            "source": "claude/common_transcript",
            "role": "user",
            "content": f"msg-{i}",
        }
        for i in range(5)
    ]
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-tail-test", events=numbered_events
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-tail-test", "--tail", "2", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == "msg-3"
    assert json.loads(lines[1])["content"] == "msg-4"


def test_transcript_cli_applies_head(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    numbered_events = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "user_message",
            "event_id": f"e{i}",
            "source": "claude/common_transcript",
            "role": "user",
            "content": f"msg-{i}",
        }
        for i in range(5)
    ]
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-head-test", events=numbered_events
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-head-test", "--head", "2", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == "msg-0"
    assert json.loads(lines[1])["content"] == "msg-1"


def test_transcript_cli_rejects_agent_type_without_mixin(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """Agent types whose class does not implement HasCommonTranscriptMixin should be rejected up front.

    The default 'generic' agent_type maps to the BaseAgent default class, which
    does not implement the mixin -- the CLI must fail with a clear error
    naming the agent and its type, rather than a misleading 'no transcript yet' message.
    """
    create_agent_with_events_dir(local_provider.host_dir, agent_name="no-transcript-agent")

    result = cli_runner.invoke(
        transcript,
        ["no-transcript-agent"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "no-transcript-agent" in result.output
    assert "generic" in result.output
    assert "does not produce a common transcript" in result.output


def test_transcript_cli_missing_events_file_for_supporting_type_gives_clear_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """A supporting agent type with no transcript events yet still gets the 'no source' error.

    The mixin precheck passes (the type implements it), but the on-disk file is
    missing, so the original 'No common transcript found' error path runs.
    """
    create_agent_with_events_dir(local_provider.host_dir, agent_name="claude-pending-agent", agent_type="claude")

    result = cli_runner.invoke(
        transcript,
        ["claude-pending-agent"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "No common transcript found" in result.output


def _register_subtype_in_settings(settings_path: Path, type_name: str, parent_type: str) -> None:
    """Register a config-defined subtype with a ``parent_type`` in a fresh settings.toml.

    Mirrors create_test's ``_write_agent_type_command_to_settings`` but writes a
    ``parent_type`` (rather than a ``command``), producing a custom type whose class
    is inherited from its parent. ``is_allowed_in_pytest`` opts the config into the run.
    """
    settings_doc = load_config_file_tomlkit(settings_path)
    settings_doc["is_allowed_in_pytest"] = True
    type_table = tomlkit.table()
    type_table["parent_type"] = parent_type
    agent_types = tomlkit.table()
    agent_types[type_name] = type_table
    settings_doc["agent_types"] = agent_types
    save_config_file(settings_path, settings_doc)


def test_transcript_cli_resolves_config_subtype_through_parent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_host_dir,
) -> None:
    """A config-defined subtype (parent_type='claude') resolves to its parent's class.

    Regression: transcript used a flat ``get_agent_class`` lookup that only knew
    plugin-registered types, so a custom ``[agent_types.X]`` with parent_type='claude'
    failed up front with "Unknown agent type 'X'". It must instead resolve through the
    parent chain (like every other command) and read the parent's transcript.
    """
    _register_subtype_in_settings(get_or_create_profile_dir(temp_host_dir) / "settings.toml", "coder", "claude")
    _agent_id, events_dir = create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="coder-agent",
        events_source="claude/common_transcript",
        agent_type="coder",
    )
    write_common_transcript_events(events_dir, SAMPLE_TRANSCRIPT_EVENTS)

    result = cli_runner.invoke(
        transcript,
        ["coder-agent"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0, result.output
    assert "Hello" in result.output
    assert "World" in result.output


def test_transcript_cli_blocks_unresolvable_agent_type(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """An agent whose type does not resolve at all must be blocked, not silently read.

    The precheck exists to refuse types we do not know how to read. A type that
    is neither registered nor defined in config (e.g. its plugin was uninstalled)
    must fail fast with the resolver's clear error rather than falling through to
    transcript discovery.
    """
    create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="orphan-type-agent",
        agent_type="definitely-unregistered-type",
    )

    result = cli_runner.invoke(
        transcript,
        ["orphan-type-agent"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "definitely-unregistered-type" in result.output
