import json
from pathlib import Path
from typing import Any

import pluggy
import pytest
import tomlkit
from click.testing import CliRunner

from imbue.mngr.agents.data_types.atif.trajectory import Trajectory
from imbue.mngr.cli.testing import LEGACY_SAMPLE_TRANSCRIPT_EVENTS
from imbue.mngr.cli.testing import SAMPLE_ATIF_STREAM_EVENTS
from imbue.mngr.cli.testing import create_agent_with_events_dir
from imbue.mngr.cli.testing import create_agent_with_sample_transcript
from imbue.mngr.cli.testing import write_common_transcript_events
from imbue.mngr.cli.transcript import TranscriptCliOptions
from imbue.mngr.cli.transcript import _REASONING_DISPLAY_LIMIT
from imbue.mngr.cli.transcript import _TOOL_OUTPUT_DISPLAY_LIMIT
from imbue.mngr.cli.transcript import _format_event_human
from imbue.mngr.cli.transcript import _get_event_role
from imbue.mngr.cli.transcript import _parse_transcript_events
from imbue.mngr.cli.transcript import _render_content
from imbue.mngr.cli.transcript import transcript
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.errors import MngrError
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
        output=None,
        full=False,
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
    opts = _make_transcript_opts(role=("user", "agent"))
    assert opts.role == ("user", "agent")


def test_transcript_cli_options_with_tail() -> None:
    opts = _make_transcript_opts(tail=10)
    assert opts.tail == 10


def test_transcript_cli_options_with_head() -> None:
    opts = _make_transcript_opts(head=5)
    assert opts.head == 5


# =============================================================================
# _get_event_role tests
# =============================================================================


def test_get_event_role_maps_atif_records() -> None:
    assert _get_event_role({"type": "step", "source": "user"}) == "user"
    assert _get_event_role({"type": "step", "source": "agent"}) == "agent"
    assert _get_event_role({"type": "step", "source": "system"}) == "system"
    assert _get_event_role({"type": "observation", "results": []}) == "tool"
    assert _get_event_role({"type": "header", "schema_version": "ATIF-v1.7"}) is None


def test_get_event_role_returns_none_for_unknown_type() -> None:
    assert _get_event_role({"type": "something_else"}) is None


def test_get_event_role_returns_none_for_empty_event() -> None:
    assert _get_event_role({}) is None


# =============================================================================
# _parse_transcript_events tests
# =============================================================================


def _parse_events(content: str, roles: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return _parse_transcript_events(
        content,
        roles=roles,
        source_description="transcript file 'events.jsonl' for agent 'test-agent'",
    )


def _user_step_line(message: str) -> str:
    return json.dumps({"type": "step", "source": "user", "message": message})


def _agent_step_line(message: str) -> str:
    return json.dumps({"type": "step", "source": "agent", "message": message})


def test_parse_transcript_events_parses_jsonl_lines() -> None:
    content = _user_step_line("hello") + "\n" + _agent_step_line("hi") + "\n"
    events = _parse_events(content)
    assert [event["source"] for event in events] == ["user", "agent"]


def test_parse_transcript_events_filters_by_role() -> None:
    content = (
        _user_step_line("hello")
        + "\n"
        + _agent_step_line("hi")
        + "\n"
        + json.dumps({"type": "observation", "results": [{"source_call_id": "c1", "content": "ok"}]})
        + "\n"
    )
    events = _parse_events(content, roles=("user",))
    assert len(events) == 1
    assert events[0]["message"] == "hello"


def test_parse_transcript_events_filters_multiple_roles() -> None:
    content = (
        _user_step_line("hello")
        + "\n"
        + _agent_step_line("hi")
        + "\n"
        + json.dumps({"type": "observation", "results": [{"source_call_id": "c1", "content": "ok"}]})
        + "\n"
    )
    events = _parse_events(content, roles=("user", "tool"))
    assert [event["type"] for event in events] == ["step", "observation"]


def test_parse_transcript_events_skips_blank_lines() -> None:
    content = "\n\n" + _user_step_line("hello") + "\n\n"
    assert len(_parse_events(content)) == 1


def test_parse_transcript_events_skips_malformed_json() -> None:
    content = "not json\n" + _user_step_line("hello") + "\n"
    # Mid-file malformed lines now emit a logger.warning; absorb it so it doesn't
    # leak to uncaptured output. The dedicated mid-file warning test asserts on it.
    with capture_loguru(level="WARNING"):
        events = _parse_events(content)
    assert len(events) == 1


def test_parse_transcript_events_warns_on_mid_file_corruption() -> None:
    content = _user_step_line("hello") + "\n" + "this is not json {{{\n" + _agent_step_line("hi") + "\n"
    with capture_loguru(level="WARNING") as log_output:
        events = _parse_events(content)
    assert len(events) == 2
    assert "Skipped corrupt JSONL line" in log_output.getvalue()


def test_parse_transcript_events_silent_on_partial_last_line() -> None:
    content = _user_step_line("hello") + "\nincomplete{"
    with capture_loguru(level="WARNING") as log_output:
        events = _parse_events(content)
    assert len(events) == 1
    assert log_output.getvalue() == ""


def test_parse_transcript_events_warns_when_a_role_filter_matches_nothing() -> None:
    content = _user_step_line("hello") + "\n"
    with capture_loguru(level="WARNING") as log_output:
        events = _parse_events(content, roles=("assistant",))
    assert events == []
    assert "--role assistant" in log_output.getvalue()
    assert "user, agent, system, tool" in log_output.getvalue()


def test_parse_transcript_events_rejects_an_old_format_stream() -> None:
    content = "\n".join(json.dumps(event) for event in LEGACY_SAMPLE_TRANSCRIPT_EVENTS) + "\n"

    with pytest.raises(MngrError) as exc_info:
        _parse_events(content)

    assert "agent 'test-agent'" in str(exc_info.value)
    assert "pre-ATIF" in str(exc_info.value)


def test_parse_transcript_events_rejects_an_old_format_stream_even_when_filtering() -> None:
    # The role filter must not hide the old-format error: legacy records map to no
    # ATIF role, so filtering would otherwise silently yield an empty transcript.
    content = json.dumps(LEGACY_SAMPLE_TRANSCRIPT_EVENTS[1]) + "\n"

    with pytest.raises(MngrError):
        _parse_events(content, roles=("agent",))


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
    assert "agent:" in result.output


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
    assert len(lines) == len(SAMPLE_ATIF_STREAM_EVENTS)
    parsed = json.loads(lines[1])
    assert parsed["type"] == "step"
    assert parsed["message"] == "Hello"


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
    assert len(parsed) == len(SAMPLE_ATIF_STREAM_EVENTS)


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
    assert parsed["source"] == "user"


def test_transcript_cli_filters_by_multiple_roles(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-multirole-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-multirole-test", "--role", "user", "--role", "agent", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2


def _numbered_user_steps(count: int) -> list[dict[str, Any]]:
    """``count`` ATIF user steps whose messages are ``msg-0`` .. ``msg-<count-1>``."""
    return [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "step",
            "event_id": f"e{i}",
            "emitter": "claude/common_transcript",
            "source": "user",
            "message": f"msg-{i}",
        }
        for i in range(count)
    ]


def test_transcript_cli_applies_tail(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    numbered_events = _numbered_user_steps(5)
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
    assert json.loads(lines[0])["message"] == "msg-3"
    assert json.loads(lines[1])["message"] == "msg-4"


def test_transcript_cli_applies_head(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    numbered_events = _numbered_user_steps(5)
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
    assert json.loads(lines[0])["message"] == "msg-0"
    assert json.loads(lines[1])["message"] == "msg-1"


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
    write_common_transcript_events(events_dir, SAMPLE_ATIF_STREAM_EVENTS)

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


# =============================================================================
# --format atif (doc-builder) tests
# =============================================================================


def test_transcript_cli_builds_atif_document(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-test", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-test", "--format", "atif"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    trajectory = Trajectory.model_validate(document)
    assert trajectory.schema_version == "ATIF-v1.7"
    assert trajectory.agent.name == "claude"
    assert trajectory.agent.version == "unknown"
    assert [step.step_id for step in trajectory.steps] == [1, 2]
    agent_step = trajectory.steps[1]
    assert agent_step.observation is not None
    assert agent_step.observation.results[0].source_call_id == "call_1"


def test_transcript_cli_atif_writes_output_file(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
    tmp_path: Path,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-out-test", events=SAMPLE_ATIF_STREAM_EVENTS
    )
    output_path = tmp_path / "trajectory.json"

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-out-test", "--format", "atif", "--output", str(output_path)],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    trajectory = Trajectory.model_validate(json.loads(output_path.read_text()))
    assert len(trajectory.steps) == 2


def test_transcript_cli_atif_rejects_old_format_stream(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-legacy-test", events=LEGACY_SAMPLE_TRANSCRIPT_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-legacy-test", "--format", "atif"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "pre-ATIF" in result.output


def test_transcript_cli_atif_rejects_role_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-role-test", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-role-test", "--format", "atif", "--role", "user"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "do not apply to --format atif" in result.output


def test_transcript_cli_output_flag_requires_atif_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
    tmp_path: Path,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-output-flag-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-output-flag-test", "--output", str(tmp_path / "x.json")],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--output is only supported with --format atif" in result.output


def test_transcript_cli_atif_embeds_proxy_subagent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    # A parent whose agent step delegates via a Task-style tool call...
    parent_events = [
        SAMPLE_ATIF_STREAM_EVENTS[0],
        SAMPLE_ATIF_STREAM_EVENTS[1],
        {
            "type": "step",
            "event_id": "a1-assistant",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-01-01T00:00:01Z",
            "source": "agent",
            "message": "delegating",
            "tool_calls": [
                {"tool_call_id": "toolu_task_1", "function_name": "Task", "arguments": {"prompt": "subtask"}}
            ],
        },
        {
            "type": "observation",
            "event_id": "a1-tool_result-toolu_task_1",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-01-01T00:00:05Z",
            "results": [{"source_call_id": "toolu_task_1", "content": "subagent summary"}],
        },
    ]
    parent_id, parent_events_dir = create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="transcript-atif-parent",
        events_source="claude/common_transcript",
        agent_type="claude",
    )
    write_common_transcript_events(parent_events_dir, parent_events)
    # ...and a sibling proxy child carrying the parent-link labels.
    _child_id, child_events_dir = create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="transcript-atif-child",
        events_source="claude/common_transcript",
        agent_type="claude",
        labels={
            "mngr_claude_subagent_proxy_parent_id": str(parent_id),
            "mngr_claude_subagent_proxy_tool_use_id": "toolu_task_1",
        },
    )
    write_common_transcript_events(child_events_dir, SAMPLE_ATIF_STREAM_EVENTS)

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-parent", "--format", "atif"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    trajectory = Trajectory.model_validate(json.loads(result.output))
    assert trajectory.subagent_trajectories is not None
    embedded = trajectory.subagent_trajectories[0]
    assert embedded.extra is not None and embedded.extra["subagent_kind"] == "mngr"
    assert len(embedded.steps) == 2
    delegating_observation = trajectory.steps[1].observation
    assert delegating_observation is not None
    delegating_result = delegating_observation.results[0]
    assert delegating_result.subagent_trajectory_ref is not None
    assert delegating_result.subagent_trajectory_ref[0].trajectory_id == embedded.trajectory_id
    assert delegating_result.content == "subagent summary"


def test_transcript_cli_atif_rejects_head_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-head-test", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-head-test", "--format", "atif", "--head", "1"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "do not apply to --format atif" in result.output


def test_transcript_cli_atif_rejects_tail_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-tail-test", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-tail-test", "--format", "atif", "--tail", "1"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "do not apply to --format atif" in result.output


def test_transcript_cli_atif_rejects_host_target(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """A whole-host target has no single trajectory to build, so --format atif must refuse it."""
    result = cli_runner.invoke(
        transcript,
        ["@some-host", "--format", "atif"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "requires an agent target" in result.output


def test_transcript_cli_atif_honors_a_config_default_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_host_dir,
) -> None:
    """`[commands.transcript] output_format = "atif"` must reach the doc-builder, not the template path.

    Regression: --format atif used to be intercepted from the raw click params
    before config defaults were applied, so a configured default was rejected as
    an invalid format template.
    """
    settings_path = get_or_create_profile_dir(temp_host_dir) / "settings.toml"
    settings_doc = load_config_file_tomlkit(settings_path)
    settings_doc["is_allowed_in_pytest"] = True
    transcript_defaults = tomlkit.table()
    transcript_defaults["output_format"] = "atif"
    commands = tomlkit.table()
    commands["transcript"] = transcript_defaults
    settings_doc["commands"] = commands
    save_config_file(settings_path, settings_doc)
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-config-test", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-config-test"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    trajectory = Trajectory.model_validate(json.loads(result.output))
    assert [step.step_id for step in trajectory.steps] == [1, 2]


def _make_parent_stream_delegating_to(tool_use_id: str) -> list[dict[str, Any]]:
    """An ATIF stream whose agent step delegates through ``tool_use_id`` and gets a result."""
    return [
        SAMPLE_ATIF_STREAM_EVENTS[0],
        SAMPLE_ATIF_STREAM_EVENTS[1],
        {
            "type": "step",
            "event_id": f"a1-assistant-{tool_use_id}",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-01-01T00:00:01Z",
            "source": "agent",
            "message": "delegating",
            "tool_calls": [{"tool_call_id": tool_use_id, "function_name": "Task", "arguments": {"prompt": "subtask"}}],
        },
        {
            "type": "observation",
            "event_id": f"a1-tool_result-{tool_use_id}",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-01-01T00:00:05Z",
            "results": [{"source_call_id": tool_use_id, "content": "subagent summary"}],
        },
    ]


def test_transcript_cli_atif_skips_a_legacy_format_subagent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """A child on a pre-ATIF emitter is skipped with a warning; the parent still builds."""
    parent_id, parent_events_dir = create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="transcript-atif-legacy-child-parent",
        events_source="claude/common_transcript",
        agent_type="claude",
    )
    write_common_transcript_events(parent_events_dir, _make_parent_stream_delegating_to("toolu_legacy_child"))
    _child_id, child_events_dir = create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="transcript-atif-legacy-child",
        events_source="claude/common_transcript",
        agent_type="claude",
        labels={
            "mngr_claude_subagent_proxy_parent_id": str(parent_id),
            "mngr_claude_subagent_proxy_tool_use_id": "toolu_legacy_child",
        },
    )
    write_common_transcript_events(child_events_dir, LEGACY_SAMPLE_TRANSCRIPT_EVENTS)

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-legacy-child-parent", "--format", "atif"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    trajectory = Trajectory.model_validate(json.loads(result.stdout))
    assert trajectory.subagent_trajectories is None
    delegating_observation = trajectory.steps[1].observation
    assert delegating_observation is not None
    assert delegating_observation.results[0].content == "subagent summary"
    assert delegating_observation.results[0].subagent_trajectory_ref is None


def test_transcript_cli_atif_embeds_a_grandchild_recursively(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """A parent -> child -> grandchild proxy chain embeds all the way down."""
    parent_id, parent_events_dir = create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="transcript-atif-gp",
        events_source="claude/common_transcript",
        agent_type="claude",
    )
    write_common_transcript_events(parent_events_dir, _make_parent_stream_delegating_to("toolu_child"))
    child_id, child_events_dir = create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="transcript-atif-gc-child",
        events_source="claude/common_transcript",
        agent_type="claude",
        labels={
            "mngr_claude_subagent_proxy_parent_id": str(parent_id),
            "mngr_claude_subagent_proxy_tool_use_id": "toolu_child",
        },
    )
    write_common_transcript_events(child_events_dir, _make_parent_stream_delegating_to("toolu_grandchild"))
    _grandchild_id, grandchild_events_dir = create_agent_with_events_dir(
        local_provider.host_dir,
        agent_name="transcript-atif-grandchild",
        events_source="claude/common_transcript",
        agent_type="claude",
        labels={
            "mngr_claude_subagent_proxy_parent_id": str(child_id),
            "mngr_claude_subagent_proxy_tool_use_id": "toolu_grandchild",
        },
    )
    write_common_transcript_events(grandchild_events_dir, SAMPLE_ATIF_STREAM_EVENTS)

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-gp", "--format", "atif"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    trajectory = Trajectory.model_validate(json.loads(result.stdout))
    assert trajectory.subagent_trajectories is not None
    embedded_child = trajectory.subagent_trajectories[0]
    assert str(embedded_child.trajectory_id) == str(child_id)
    assert embedded_child.subagent_trajectories is not None
    embedded_grandchild = embedded_child.subagent_trajectories[0]
    assert embedded_grandchild.extra is not None and embedded_grandchild.extra["subagent_kind"] == "mngr"
    assert len(embedded_grandchild.steps) == 2


# =============================================================================
# _format_event_human tests
# =============================================================================


def test_format_atif_agent_step_shows_message_thinking_and_tool_calls() -> None:
    event = {
        "type": "step",
        "timestamp": "2026-01-01T00:00:01Z",
        "source": "agent",
        "message": "running it",
        "reasoning_content": "the user wants the thing",
        "tool_calls": [{"tool_call_id": "c1", "function_name": "Bash", "arguments": {"command": "echo hi"}}],
    }

    result = _format_event_human(event, is_full=False)

    assert result.startswith("[2026-01-01T00:00:01Z] agent:")
    assert "(thinking) the user wants the thing" in result
    assert "running it" in result
    assert '-> Bash({"command":"echo hi"})' in result


def test_format_atif_step_truncates_tool_arguments_unless_full() -> None:
    long_command = "x" * 500
    event = {
        "type": "step",
        "timestamp": "2026-01-01T00:00:01Z",
        "source": "agent",
        "message": "",
        "tool_calls": [{"tool_call_id": "c1", "function_name": "Bash", "arguments": {"command": long_command}}],
    }

    truncated = _format_event_human(event, is_full=False)
    full = _format_event_human(event, is_full=True)

    assert "..." in truncated
    assert long_command not in truncated
    assert long_command in full


def test_format_atif_observation_shows_tool_name_error_and_output() -> None:
    event = {
        "type": "observation",
        "timestamp": "2026-01-01T00:00:02Z",
        "results": [
            {"source_call_id": "c1", "content": "boom", "extra": {"is_error": True, "tool_name": "Bash"}},
        ],
    }

    result = _format_event_human(event, is_full=False)

    assert "tool (Bash) [ERROR]:" in result
    assert "boom" in result


def test_format_atif_observation_truncates_output_unless_full() -> None:
    long_output = "y" * 3000
    event = {
        "type": "observation",
        "timestamp": "2026-01-01T00:00:02Z",
        "results": [{"source_call_id": "c1", "content": long_output, "extra": {"tool_name": "Bash"}}],
    }

    truncated = _format_event_human(event, is_full=False)
    full = _format_event_human(event, is_full=True)

    assert truncated.endswith("y" * _TOOL_OUTPUT_DISPLAY_LIMIT + "...")
    assert long_output in full


def test_format_atif_system_step_renders_inline_observation() -> None:
    event = {
        "type": "step",
        "timestamp": "2026-01-01T00:00:03Z",
        "source": "system",
        "message": "Context compaction performed",
        "observation": {"results": [{"content": "Summary: prior context."}]},
    }

    result = _format_event_human(event, is_full=False)

    assert result.startswith("[2026-01-01T00:00:03Z] system:")
    assert "Context compaction performed" in result
    assert "Summary: prior context." in result


def test_transcript_cli_renders_atif_stream_human(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-human-test", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-human-test"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert "user:" in result.output
    assert "Hello" in result.output
    assert "agent:" in result.output
    assert "World" in result.output
    assert '-> Bash({"command":"echo ok"})' in result.output
    assert "tool (Bash):" in result.output
    # The header line is stream framing, not conversation content.
    assert "ATIF-v1.7" not in result.output


def test_transcript_cli_filters_atif_stream_by_role(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-role-filter-test", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-role-filter-test", "--role", "agent", "--format", "jsonl"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "step"
    assert parsed["source"] == "agent"


def test_format_atif_step_truncates_reasoning_unless_full() -> None:
    long_reasoning = "z" * 900
    event = {
        "type": "step",
        "timestamp": "2026-01-01T00:00:01Z",
        "source": "agent",
        "reasoning_content": long_reasoning,
    }

    truncated = _format_event_human(event, is_full=False)
    full = _format_event_human(event, is_full=True)

    assert truncated.endswith("z" * _REASONING_DISPLAY_LIMIT + "...")
    assert long_reasoning in full


def test_format_atif_step_without_content_is_no_content() -> None:
    event = {"type": "step", "timestamp": "2026-01-01T00:00:01Z", "source": "agent"}

    result = _format_event_human(event, is_full=False)

    assert result == "[2026-01-01T00:00:01Z] agent:\n(no content)"


def test_format_atif_observation_without_results_says_no_results() -> None:
    event = {"type": "observation", "timestamp": "2026-01-01T00:00:02Z", "results": []}

    result = _format_event_human(event, is_full=False)

    assert result == "[2026-01-01T00:00:02Z] tool: (no results)"


def test_render_content_renders_text_and_image_parts() -> None:
    rendered = _render_content(
        [
            {"type": "text", "text": "before the screenshot"},
            {"type": "image", "source": {"media_type": "image/png", "path": "images/shot.png"}},
            {"type": "text", "text": "after the screenshot"},
        ]
    )

    assert rendered == "before the screenshot\n[image: image/png]\nafter the screenshot"


def test_render_content_passes_a_plain_string_through() -> None:
    assert _render_content("just text") == "just text"


def test_transcript_cli_head_skips_the_atif_header_in_human_output(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """--head counts conversation records in human output, not the stream header."""
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-head-human", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-head-human", "--head", "1"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert "user:" in result.output
    assert "Hello" in result.output
    assert "agent:" not in result.output


def test_transcript_cli_head_keeps_the_atif_header_in_jsonl(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """--format jsonl emits the raw stream, so its window still includes the header."""
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-atif-head-jsonl", events=SAMPLE_ATIF_STREAM_EVENTS
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-atif-head-jsonl", "--head", "2", "--format", "jsonl"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert [json.loads(line)["type"] for line in lines] == ["header", "step"]


def test_transcript_cli_full_disables_output_truncation(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    long_output = "q" * 3000
    events = [dict(event) for event in SAMPLE_ATIF_STREAM_EVENTS]
    events[-1] = {
        **events[-1],
        "results": [{"source_call_id": "call_1", "content": long_output, "extra": {"tool_name": "Bash"}}],
    }
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-atif-full-test", events=events)

    truncated = cli_runner.invoke(transcript, ["transcript-atif-full-test"], obj=plugin_manager)
    full = cli_runner.invoke(transcript, ["transcript-atif-full-test", "--full"], obj=plugin_manager)

    assert truncated.exit_code == 0, truncated.output
    assert full.exit_code == 0, full.output
    assert long_output not in truncated.output
    assert "q" * _TOOL_OUTPUT_DISPLAY_LIMIT + "..." in truncated.output
    assert long_output in full.output


def _atif_stream_with_system_step() -> list[dict[str, Any]]:
    """The sample ATIF stream plus a system step, so every ATIF role is present."""
    return [
        *SAMPLE_ATIF_STREAM_EVENTS,
        {
            "type": "step",
            "event_id": "s1-system",
            "emitter": "claude/common_transcript",
            "timestamp": "2026-01-01T00:00:03Z",
            "source": "system",
            "message": "Context compaction performed",
            "observation": {"results": [{"content": "Summary: prior context."}]},
        },
    ]


def test_transcript_cli_filters_atif_stream_by_each_role(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(
        local_provider.host_dir,
        agent_name="transcript-atif-all-roles-test",
        events=_atif_stream_with_system_step(),
    )

    def _types_and_sources(role: str) -> list[tuple[str, str | None]]:
        result = cli_runner.invoke(
            transcript,
            ["transcript-atif-all-roles-test", "--role", role, "--format", "jsonl"],
            obj=plugin_manager,
        )
        assert result.exit_code == 0, result.output
        parsed = [json.loads(line) for line in result.output.strip().split("\n") if line.strip()]
        return [(event["type"], event.get("source")) for event in parsed]

    assert _types_and_sources("user") == [("step", "user")]
    assert _types_and_sources("system") == [("step", "system")]
    assert _types_and_sources("tool") == [("observation", None)]
