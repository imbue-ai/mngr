"""Unit tests for GeminiAgentConfig and GeminiAgent."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.errors import AgentStartError
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_gemini.gemini_config import HOOK_EVENT_BEFORE_TOOL
from imbue.mngr_gemini.gemini_config import HOOK_EVENT_SESSION_START
from imbue.mngr_gemini.plugin import GeminiAgent
from imbue.mngr_gemini.plugin import GeminiAgentConfig
from imbue.mngr_gemini.plugin import register_agent_type


def test_gemini_agent_config_has_correct_defaults() -> None:
    """Verify that GeminiAgentConfig has the expected default values."""
    config = GeminiAgentConfig()

    assert str(config.command) == "gemini"
    assert config.cli_args == ()
    assert config.permissions == []
    assert config.parent_type is None
    assert config.emit_common_transcript is True
    assert config.auto_allow_permissions is False


def test_gemini_agent_config_merge_with_concatenates_user_args() -> None:
    """User-supplied cli_args concatenate onto the (empty) default."""
    base = GeminiAgentConfig()
    override = GeminiAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, GeminiAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "gemini"


def test_gemini_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(GeminiAgent, InteractiveTuiAgent)


def test_gemini_agent_advertises_tui_ready_indicator() -> None:
    """Ready indicator is the stable header banner."""
    assert GeminiAgent.TUI_READY_INDICATOR == "Gemini CLI"


def test_gemini_agent_uses_input_cleared_placeholder_for_submission_confirmation() -> None:
    """The poll-and-retry strategy is configured with the input-row placeholder."""
    assert GeminiAgent.INPUT_CLEARED_INDICATOR == "Type your message"


def test_gemini_agent_implements_send_enter_and_validate() -> None:
    """GeminiAgent fills in the abstract method by picking a strategy."""
    assert "_send_enter_and_validate" not in GeminiAgent.__abstractmethods__


def test_register_agent_type_returns_gemini_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "gemini"
    assert agent_class is GeminiAgent
    assert config_class is GeminiAgentConfig


def _make_gemini_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: GeminiAgentConfig,
) -> GeminiAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return GeminiAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-gemini"),
        agent_type=AgentTypeName("gemini"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


@pytest.fixture
def gemini_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> GeminiAgent:
    return _make_gemini_agent(local_provider, tmp_path, GeminiAgentConfig())


@pytest.fixture
def gemini_agent_without_transcript(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> GeminiAgent:
    return _make_gemini_agent(local_provider, tmp_path, GeminiAgentConfig(emit_common_transcript=False))


@pytest.fixture
def gemini_agent_auto_allow(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> GeminiAgent:
    return _make_gemini_agent(local_provider, tmp_path, GeminiAgentConfig(auto_allow_permissions=True))


def _provision(agent: GeminiAgent) -> None:
    """Run provision with the standard options used throughout these tests."""
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("gemini")),
        mngr_ctx=agent.mngr_ctx,
    )


def test_assemble_command_uses_bare_gemini_command_with_no_default_cli_args(
    gemini_agent: GeminiAgent,
) -> None:
    command = gemini_agent.assemble_command(gemini_agent.host, (), command_override=None)
    assert str(command).endswith("gemini")


def test_assemble_command_appends_user_agent_args_after_cli_args(gemini_agent: GeminiAgent) -> None:
    command = gemini_agent.assemble_command(gemini_agent.host, ("--debug",), command_override=None)
    assert str(command).endswith("gemini --debug")


_SENTINEL_RM_THEN_GEMINI = "rm -f $MNGR_AGENT_STATE_DIR/session_started && gemini"


def test_assemble_command_clears_stale_sentinel_before_launching_gemini_with_watcher(
    gemini_agent: GeminiAgent,
) -> None:
    """The sentinel rm must sit between the backgrounded watcher and ``gemini``.

    Bash's ``A && B & C`` precedence parses as ``( A && B ) &`` followed by
    ``C``, so writing ``rm && ( watcher ) & gemini`` would push the rm into
    the background where it races gemini's startup. Putting the watcher
    first confines ``&`` to the watcher subshell and leaves
    ``rm && gemini`` as a foreground sequential chain.
    """
    command = str(gemini_agent.assemble_command(gemini_agent.host, (), command_override=None))
    assert command.endswith(_SENTINEL_RM_THEN_GEMINI), command


def test_assemble_command_clears_stale_sentinel_before_launching_gemini_without_watcher(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    """Without the common watcher, the raw streamer is the head, then ``rm && gemini``."""
    agent = gemini_agent_without_transcript
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    assert command.endswith(_SENTINEL_RM_THEN_GEMINI), command


def test_assemble_command_prepends_transcript_watcher_when_enabled(gemini_agent: GeminiAgent) -> None:
    command = str(gemini_agent.assemble_command(gemini_agent.host, (), command_override=None))
    assert "$MNGR_AGENT_STATE_DIR/commands/common_transcript.sh" in command
    # Raw-streamer subshell must come FIRST (it is required by
    # HasTranscriptMixin and runs even when the common watcher is off), then
    # the common-watcher subshell, then the foreground ``rm && gemini``
    # chain. Background subshells before the rm guarantees ``&`` only
    # terminates each watcher and not the rm itself.
    assert command.startswith("( bash $MNGR_AGENT_STATE_DIR/commands/stream_transcript.sh ) &")
    assert "( bash $MNGR_AGENT_STATE_DIR/commands/common_transcript.sh ) &" in command


def test_assemble_command_always_prepends_raw_streamer(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    """The raw streamer is required by HasTranscriptMixin and runs even when the common watcher is off."""
    agent = gemini_agent_without_transcript
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    assert command.startswith("( bash $MNGR_AGENT_STATE_DIR/commands/stream_transcript.sh ) &")


def test_assemble_command_skips_common_transcript_watcher_when_disabled(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    agent = gemini_agent_without_transcript
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    assert "common_transcript.sh" not in command
    assert command.endswith("gemini")


def test_get_expected_process_name_returns_node(gemini_agent: GeminiAgent) -> None:
    """gemini-cli is a node script with no process.title override -- ps shows 'node'."""
    assert gemini_agent.get_expected_process_name() == "node"


def test_get_readiness_sentinel_path_lives_in_per_agent_state_dir(gemini_agent: GeminiAgent) -> None:
    """The sentinel path matches the file the SessionStart hook touches at runtime."""
    sentinel = gemini_agent._get_readiness_sentinel_path()
    assert sentinel == gemini_agent._get_agent_dir() / "session_started"


def test_wait_for_ready_signal_returns_when_sentinel_appears(gemini_agent: GeminiAgent) -> None:
    """Polling succeeds once the SessionStart hook has touched the sentinel file."""
    # Simulate the SessionStart hook having already fired by creating the
    # sentinel before start_action runs.
    sentinel = gemini_agent._get_readiness_sentinel_path()
    sentinel.parent.mkdir(parents=True, exist_ok=True)

    start_action_invocations = []

    def start_action() -> None:
        start_action_invocations.append(None)
        sentinel.touch()

    # is_creating=False skips the TUI-banner poll the super().wait_for_ready_signal does,
    # which we can't reasonably satisfy in a unit test (no real tmux pane).
    gemini_agent.wait_for_ready_signal(is_creating=False, start_action=start_action, timeout=2.0)
    assert start_action_invocations == [None]
    assert sentinel.exists()


def test_wait_for_ready_signal_raises_when_sentinel_never_appears(
    gemini_agent: GeminiAgent,
) -> None:
    """If the SessionStart hook never fires, surface a clear AgentStartError."""
    sentinel = gemini_agent._get_readiness_sentinel_path()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    assert not sentinel.exists()

    with pytest.raises(AgentStartError) as excinfo:
        gemini_agent.wait_for_ready_signal(is_creating=False, start_action=lambda: None, timeout=0.2)
    message = str(excinfo.value)
    # The diagnostic must report the timeout value so operators can tell
    # whether the budget was too short, and must name at least one of the
    # env vars the hook depends on so the most likely fix is discoverable.
    assert "0.2" in message
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" in message or "GEMINI_CLI_TRUST_WORKSPACE" in message


def test_get_common_transcript_scripts_returns_common_transcript_sh(gemini_agent: GeminiAgent) -> None:
    """The mixin's required script set is keyed by filename and contains the converter body."""
    scripts = gemini_agent.get_common_transcript_scripts()
    assert "common_transcript.sh" in scripts
    body = scripts["common_transcript.sh"]
    assert body.startswith("#!/usr/bin/env bash")
    assert "events/gemini/common_transcript/events.jsonl" in body


def test_get_raw_transcript_scripts_returns_stream_transcript_sh(gemini_agent: GeminiAgent) -> None:
    """The raw-transcript mixin returns the streamer that tails gemini's session JSONL."""
    scripts = gemini_agent.get_raw_transcript_scripts()
    assert "stream_transcript.sh" in scripts
    body = scripts["stream_transcript.sh"]
    assert body.startswith("#!/usr/bin/env bash")
    assert "logs/gemini_transcript/events.jsonl" in body


def test_provision_with_emit_disabled_does_not_write_common_script(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    """Disabling emit_common_transcript suppresses the common watcher, not the raw streamer."""
    agent = gemini_agent_without_transcript
    _provision(agent)

    expected_common = agent._get_agent_dir() / "commands" / "common_transcript.sh"
    assert not expected_common.exists()


def test_provision_always_writes_raw_transcript_streamer(gemini_agent: GeminiAgent) -> None:
    """The raw streamer is required by HasTranscriptMixin and is provisioned unconditionally."""
    _provision(gemini_agent)

    expected_streamer = gemini_agent._get_agent_dir() / "commands" / "stream_transcript.sh"
    assert expected_streamer.exists()
    assert expected_streamer.read_text().startswith("#!/usr/bin/env bash")
    assert expected_streamer.stat().st_mode & 0o111


def test_provision_writes_raw_transcript_streamer_even_when_emit_disabled(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    """Raw capture is not user-gated -- it must run even when the common converter is off."""
    agent = gemini_agent_without_transcript
    _provision(agent)

    expected_streamer = agent._get_agent_dir() / "commands" / "stream_transcript.sh"
    assert expected_streamer.exists()


def test_provision_with_emit_enabled_writes_common_transcript_script(gemini_agent: GeminiAgent) -> None:
    """provision should write common_transcript.sh to the agent's commands/ directory."""
    _provision(gemini_agent)

    expected_script = gemini_agent._get_agent_dir() / "commands" / "common_transcript.sh"
    assert expected_script.exists()
    assert expected_script.read_text().startswith("#!/usr/bin/env bash")
    # Execute permissions are required for the watcher script to run.
    assert expected_script.stat().st_mode & 0o111


def test_modify_env_vars_sets_trust_workspace_true(gemini_agent: GeminiAgent) -> None:
    """The agent's env must mark the workspace as trusted so headless launches start."""
    env_vars: dict[str, str] = {}
    gemini_agent.modify_env_vars(gemini_agent.host, env_vars)
    assert env_vars["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_modify_env_vars_points_system_settings_at_plugin_scoped_per_agent_file(
    gemini_agent: GeminiAgent,
) -> None:
    """Gemini reads our system-tier settings from a plugin-scoped per-agent path.

    Mirrors ``mngr_claude``'s ``plugin/claude/anthropic/`` namespacing inside
    the per-agent state dir.
    """
    env_vars: dict[str, str] = {}
    gemini_agent.modify_env_vars(gemini_agent.host, env_vars)
    settings_path = env_vars["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]
    expected = gemini_agent._get_agent_dir() / "plugin" / "gemini" / "system_settings.json"
    assert settings_path == str(expected)
    # Never inside the user's work_dir.
    assert str(gemini_agent.work_dir) not in settings_path


def test_modify_env_vars_preserves_other_vars(gemini_agent: GeminiAgent) -> None:
    env_vars = {"PRE_EXISTING": "kept"}
    gemini_agent.modify_env_vars(gemini_agent.host, env_vars)
    assert env_vars["PRE_EXISTING"] == "kept"
    assert env_vars["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" in env_vars


def _read_system_settings(agent: GeminiAgent) -> dict[str, Any]:
    parsed: Any = json.loads(agent._get_system_settings_path().read_text())
    assert isinstance(parsed, dict)
    return parsed


def test_provision_writes_system_settings_with_readiness_hook(
    gemini_agent: GeminiAgent,
) -> None:
    """The mngr-owned system-tier settings file holds the SessionStart hook."""
    _provision(gemini_agent)
    settings = _read_system_settings(gemini_agent)
    assert HOOK_EVENT_SESSION_START in settings["hooks"]
    inner_command = settings["hooks"][HOOK_EVENT_SESSION_START][0]["hooks"][0]["command"]
    assert "session_started" in inner_command
    assert "MNGR_AGENT_STATE_DIR" in inner_command


def test_provision_does_not_create_gemini_dir_in_workspace(
    gemini_agent: GeminiAgent,
) -> None:
    """The user's work_dir must be left completely untouched by provision."""
    _provision(gemini_agent)
    assert not (gemini_agent.work_dir / ".gemini").exists()


def test_provision_is_idempotent(gemini_agent: GeminiAgent) -> None:
    """Running provision twice yields the same content (mngr owns the file)."""
    _provision(gemini_agent)
    first = _read_system_settings(gemini_agent)
    _provision(gemini_agent)
    second = _read_system_settings(gemini_agent)
    assert first == second
    assert len(second["hooks"][HOOK_EVENT_SESSION_START]) == 1


def test_provision_installs_hooks_even_when_transcript_disabled(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    """Readiness hook ships unconditionally -- decoupled from transcript emission."""
    agent = gemini_agent_without_transcript
    _provision(agent)
    settings = _read_system_settings(agent)
    assert HOOK_EVENT_SESSION_START in settings["hooks"]


def test_provision_omits_before_tool_hook_when_auto_allow_disabled(
    gemini_agent: GeminiAgent,
) -> None:
    """The default config does not install a permission auto-allow hook."""
    _provision(gemini_agent)
    settings = _read_system_settings(gemini_agent)
    assert HOOK_EVENT_BEFORE_TOOL not in settings["hooks"]


def test_provision_installs_before_tool_hook_when_auto_allow_enabled(
    gemini_agent_auto_allow: GeminiAgent,
) -> None:
    """``auto_allow_permissions=True`` adds a BeforeTool wildcard allow hook alongside readiness."""
    agent = gemini_agent_auto_allow
    _provision(agent)
    settings = _read_system_settings(agent)
    # Both hooks land in the same file.
    assert HOOK_EVENT_SESSION_START in settings["hooks"]
    assert HOOK_EVENT_BEFORE_TOOL in settings["hooks"]
    before_tool_groups = settings["hooks"][HOOK_EVENT_BEFORE_TOOL]
    assert before_tool_groups[0]["matcher"] == ".*"
    inner_command = before_tool_groups[0]["hooks"][0]["command"]
    assert '"decision":"allow"' in inner_command
