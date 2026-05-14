"""Unit tests for GeminiAgentConfig and GeminiAgent."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
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


def test_assemble_command_uses_bare_gemini_command_with_no_default_cli_args(
    gemini_agent: GeminiAgent,
) -> None:
    command = gemini_agent.assemble_command(gemini_agent.host, (), command_override=None)
    assert str(command).endswith("gemini")


def test_assemble_command_appends_user_agent_args_after_cli_args(gemini_agent: GeminiAgent) -> None:
    command = gemini_agent.assemble_command(gemini_agent.host, ("--debug",), command_override=None)
    assert str(command).endswith("gemini --debug")


def test_assemble_command_prepends_transcript_watcher_when_enabled(gemini_agent: GeminiAgent) -> None:
    command = str(gemini_agent.assemble_command(gemini_agent.host, (), command_override=None))
    assert "$MNGR_AGENT_STATE_DIR/commands/common_transcript.sh" in command
    assert command.startswith("(")


def test_assemble_command_skips_transcript_watcher_when_disabled(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    agent = gemini_agent_without_transcript
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    assert "common_transcript.sh" not in command
    assert command == "gemini"


def test_get_expected_process_name_returns_node(gemini_agent: GeminiAgent) -> None:
    """gemini-cli is a node script with no process.title override -- ps shows 'node'."""
    assert gemini_agent.get_expected_process_name() == "node"


def test_get_common_transcript_scripts_returns_common_transcript_sh(gemini_agent: GeminiAgent) -> None:
    """The mixin's required script set is keyed by filename and contains the converter body."""
    scripts = gemini_agent.get_common_transcript_scripts()
    assert "common_transcript.sh" in scripts
    body = scripts["common_transcript.sh"]
    assert body.startswith("#!/usr/bin/env bash")
    assert "events/gemini/common_transcript/events.jsonl" in body


def test_provision_with_emit_disabled_does_not_write_script(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    agent = gemini_agent_without_transcript
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("gemini")),
        mngr_ctx=agent.mngr_ctx,
    )

    # No script written because emit was disabled
    expected_script = agent._get_agent_dir() / "commands" / "common_transcript.sh"
    assert not expected_script.exists()


def test_provision_with_emit_enabled_writes_transcript_script(gemini_agent: GeminiAgent) -> None:
    """provision should write common_transcript.sh to the agent's commands/ directory."""
    gemini_agent.provision(
        host=gemini_agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("gemini")),
        mngr_ctx=gemini_agent.mngr_ctx,
    )

    expected_script = gemini_agent._get_agent_dir() / "commands" / "common_transcript.sh"
    assert expected_script.exists()
    assert expected_script.read_text().startswith("#!/usr/bin/env bash")
    # Execute permissions are required for the watcher script to run.
    assert expected_script.stat().st_mode & 0o111


def test_modify_env_vars_sets_trust_workspace_true(gemini_agent: GeminiAgent) -> None:
    """The agent's env must mark the workspace as trusted so hooks fire."""
    env_vars: dict[str, str] = {}
    gemini_agent.modify_env_vars(gemini_agent.host, env_vars)
    assert env_vars["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_modify_env_vars_preserves_other_vars(gemini_agent: GeminiAgent) -> None:
    env_vars = {"PRE_EXISTING": "kept"}
    gemini_agent.modify_env_vars(gemini_agent.host, env_vars)
    assert env_vars["PRE_EXISTING"] == "kept"
    assert env_vars["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def _read_workspace_settings(agent: GeminiAgent) -> dict[str, Any]:
    parsed: Any = json.loads((agent.work_dir / ".gemini" / "settings.json").read_text())
    assert isinstance(parsed, dict)
    return parsed


def test_provision_creates_workspace_settings_with_readiness_hook(
    gemini_agent: GeminiAgent,
) -> None:
    """A fresh workspace gets a new .gemini/settings.json containing SessionStart."""
    gemini_agent.provision(
        host=gemini_agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("gemini")),
        mngr_ctx=gemini_agent.mngr_ctx,
    )
    settings = _read_workspace_settings(gemini_agent)
    assert HOOK_EVENT_SESSION_START in settings["hooks"]
    inner_command = settings["hooks"][HOOK_EVENT_SESSION_START][0]["hooks"][0]["command"]
    assert "session_started" in inner_command
    assert "MNGR_AGENT_STATE_DIR" in inner_command


def test_provision_merges_into_existing_workspace_settings_without_clobbering(
    gemini_agent: GeminiAgent,
) -> None:
    """An existing settings.json keeps its user-managed keys after provision."""
    settings_dir = gemini_agent.work_dir / ".gemini"
    settings_dir.mkdir()
    pre_existing: dict[str, Any] = {
        "mcpServers": {"fs": {"command": "mcp-fs"}},
        "hooks": {"AfterTool": [{"hooks": [{"type": "command", "command": "echo keep"}]}]},
    }
    (settings_dir / "settings.json").write_text(json.dumps(pre_existing))

    gemini_agent.provision(
        host=gemini_agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("gemini")),
        mngr_ctx=gemini_agent.mngr_ctx,
    )

    settings = _read_workspace_settings(gemini_agent)
    assert settings["mcpServers"] == {"fs": {"command": "mcp-fs"}}
    assert settings["hooks"]["AfterTool"] == pre_existing["hooks"]["AfterTool"]
    assert HOOK_EVENT_SESSION_START in settings["hooks"]


def test_provision_is_idempotent_for_workspace_hooks(gemini_agent: GeminiAgent) -> None:
    """Running provision twice doesn't duplicate the readiness hook entry."""
    options = CreateAgentOptions(agent_type=AgentTypeName("gemini"))
    gemini_agent.provision(host=gemini_agent.host, options=options, mngr_ctx=gemini_agent.mngr_ctx)
    first = _read_workspace_settings(gemini_agent)
    gemini_agent.provision(host=gemini_agent.host, options=options, mngr_ctx=gemini_agent.mngr_ctx)
    second = _read_workspace_settings(gemini_agent)
    assert first == second
    assert len(second["hooks"][HOOK_EVENT_SESSION_START]) == 1


def test_provision_recovers_from_malformed_existing_settings(gemini_agent: GeminiAgent) -> None:
    """A user typo in settings.json must not break provisioning -- replace and continue."""
    settings_dir = gemini_agent.work_dir / ".gemini"
    settings_dir.mkdir()
    # The text below is intentionally truncated (missing closing braces).
    (settings_dir / "settings.json").write_text('{"mcpServers": {"fs":')

    gemini_agent.provision(
        host=gemini_agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("gemini")),
        mngr_ctx=gemini_agent.mngr_ctx,
    )

    settings = _read_workspace_settings(gemini_agent)
    assert HOOK_EVENT_SESSION_START in settings["hooks"]


def test_provision_installs_hooks_even_when_transcript_disabled(
    gemini_agent_without_transcript: GeminiAgent,
) -> None:
    """Readiness hook ships unconditionally -- decoupled from transcript emission."""
    agent = gemini_agent_without_transcript
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("gemini")),
        mngr_ctx=agent.mngr_ctx,
    )
    settings = _read_workspace_settings(agent)
    assert HOOK_EVENT_SESSION_START in settings["hooks"]
