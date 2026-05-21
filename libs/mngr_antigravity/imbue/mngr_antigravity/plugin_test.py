"""Unit tests for AntigravityAgentConfig and AntigravityAgent."""

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
from imbue.mngr_antigravity.antigravity_config import get_antigravity_user_settings_path
from imbue.mngr_antigravity.plugin import AntigravityAgent
from imbue.mngr_antigravity.plugin import AntigravityAgentConfig
from imbue.mngr_antigravity.plugin import register_agent_type


def test_antigravity_agent_config_has_correct_defaults() -> None:
    config = AntigravityAgentConfig()

    assert str(config.command) == "agy"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.auto_allow_permissions is False
    # Default-on: every fresh work_dir would hit the trust dialog otherwise.
    assert config.pre_trust_workspace is True


def test_antigravity_agent_config_merge_with_concatenates_user_args() -> None:
    """User-supplied cli_args concatenate onto the (empty) default."""
    base = AntigravityAgentConfig()
    override = AntigravityAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, AntigravityAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "agy"


def test_antigravity_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(AntigravityAgent, InteractiveTuiAgent)


def test_antigravity_agent_advertises_tui_ready_indicator() -> None:
    """Ready indicator is the stable splash-banner substring captured from `agy` 1.0.0."""
    assert AntigravityAgent.TUI_READY_INDICATOR == "Antigravity CLI"


def test_antigravity_agent_implements_send_enter_and_validate() -> None:
    """AntigravityAgent fills in the abstract method by picking a strategy."""
    assert "_send_enter_and_validate" not in AntigravityAgent.__abstractmethods__


def test_register_agent_type_returns_antigravity_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "antigravity"
    assert agent_class is AntigravityAgent
    assert config_class is AntigravityAgentConfig


def _make_antigravity_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: AntigravityAgentConfig,
) -> AntigravityAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return AntigravityAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-antigravity"),
        agent_type=AgentTypeName("antigravity"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


@pytest.fixture
def antigravity_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    return _make_antigravity_agent(local_provider, tmp_path, AntigravityAgentConfig())


@pytest.fixture
def antigravity_agent_auto_allow(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    return _make_antigravity_agent(local_provider, tmp_path, AntigravityAgentConfig(auto_allow_permissions=True))


@pytest.fixture
def antigravity_agent_pre_trust_disabled(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    return _make_antigravity_agent(local_provider, tmp_path, AntigravityAgentConfig(pre_trust_workspace=False))


_BACKGROUND_TASKS_LAUNCH_PREFIX = "( bash $MNGR_AGENT_STATE_DIR/commands/antigravity_background_tasks.sh"


def test_assemble_command_invokes_agy_with_log_file(antigravity_agent: AntigravityAgent) -> None:
    """The foreground command runs `agy ... --log-file <agent-state>/logs/agy_cli.log`."""
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert " agy " in command
    assert "--log-file" in command
    assert "logs/agy_cli.log" in command


def test_assemble_command_appends_user_agent_args(antigravity_agent: AntigravityAgent) -> None:
    """User agent_args land between the agy command and the appended log-file/auto-allow flags."""
    command = str(
        antigravity_agent.assemble_command(antigravity_agent.host, ("--add-dir", "/tmp"), command_override=None)
    )
    assert "agy --add-dir /tmp --log-file" in command


def test_assemble_command_omits_dangerously_skip_permissions_when_auto_allow_disabled(
    antigravity_agent: AntigravityAgent,
) -> None:
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert "--dangerously-skip-permissions" not in command


def test_assemble_command_appends_dangerously_skip_permissions_when_auto_allow_enabled(
    antigravity_agent_auto_allow: AntigravityAgent,
) -> None:
    """`auto_allow_permissions=True` wires Antigravity's documented auto-approve flag."""
    agent = antigravity_agent_auto_allow
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    assert command.endswith("--dangerously-skip-permissions")


def test_assemble_command_preserves_user_args_when_auto_allow_enabled(
    antigravity_agent_auto_allow: AntigravityAgent,
) -> None:
    """User-supplied agent_args still land before the auto-allow flag."""
    agent = antigravity_agent_auto_allow
    command = str(agent.assemble_command(agent.host, ("--add-dir", "/tmp"), command_override=None))
    assert "agy --add-dir /tmp --log-file" in command
    assert command.endswith("--dangerously-skip-permissions")


def test_assemble_command_launches_background_tasks_supervisor(antigravity_agent: AntigravityAgent) -> None:
    """The supervisor is the single backgrounded subshell; it owns the watchers."""
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert command.startswith(_BACKGROUND_TASKS_LAUNCH_PREFIX), command
    # No bare watcher subshells: the supervisor is the single entry point.
    assert "( bash $MNGR_AGENT_STATE_DIR/commands/stream_transcript.sh ) &" not in command
    assert "( bash $MNGR_AGENT_STATE_DIR/commands/common_transcript.sh ) &" not in command


def test_get_expected_process_name_returns_agy(antigravity_agent: AntigravityAgent) -> None:
    """`agy` is the single-file Go binary name visible to ps/tmux."""
    assert antigravity_agent.get_expected_process_name() == "agy"


def test_modify_env_vars_exposes_agy_log_file_path(antigravity_agent: AntigravityAgent) -> None:
    """The streamer needs the agy --log-file location to grep for `Created conversation`."""
    env_vars: dict[str, str] = {"PRE_EXISTING": "kept"}
    antigravity_agent.modify_env_vars(antigravity_agent.host, env_vars)
    assert env_vars["PRE_EXISTING"] == "kept"
    assert "ANTIGRAVITY_AGY_LOG_FILE" in env_vars
    assert env_vars["ANTIGRAVITY_AGY_LOG_FILE"].endswith("logs/agy_cli.log")
    # Must be inside the per-agent state dir so the path is unique per agent.
    assert "/agents/" in env_vars["ANTIGRAVITY_AGY_LOG_FILE"]


def test_provision_does_not_create_workspace_subdirs(antigravity_agent: AntigravityAgent) -> None:
    """The plugin writes nothing to the user's work_dir.

    Antigravity reads workspace-tier files from `<work_dir>/.agents/` and
    `<work_dir>/.antigravityignore`; mngr leaves both alone so the user's
    project tree is untouched by ``mngr create``.
    """
    antigravity_agent.provision(
        host=antigravity_agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=antigravity_agent.mngr_ctx,
    )
    assert not (antigravity_agent.work_dir / ".agents").exists()
    assert not (antigravity_agent.work_dir / ".antigravityignore").exists()
    assert not (antigravity_agent.work_dir / ".gemini").exists()


def _read_user_settings(monkeypatched_home: Path) -> dict[str, Any]:
    """Read the user-tier settings.json that the agent should have populated."""
    settings_path = monkeypatched_home / ".gemini" / "antigravity-cli" / "settings.json"
    if not settings_path.exists():
        return {}
    parsed: Any = json.loads(settings_path.read_text())
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a tmpdir so trust-file writes do not touch the user's real config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_provision_pre_trusts_workspace_in_user_settings(
    antigravity_agent: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Default `pre_trust_workspace=True` writes work_dir into trustedWorkspaces."""
    antigravity_agent.provision(
        host=antigravity_agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=antigravity_agent.mngr_ctx,
    )
    settings = _read_user_settings(isolated_home)
    assert str(antigravity_agent.work_dir) in settings["trustedWorkspaces"]


def test_provision_skips_pre_trust_when_disabled(
    antigravity_agent_pre_trust_disabled: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """``pre_trust_workspace=False`` leaves the user's settings file untouched."""
    antigravity_agent_pre_trust_disabled.provision(
        host=antigravity_agent_pre_trust_disabled.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=antigravity_agent_pre_trust_disabled.mngr_ctx,
    )
    settings_path = get_antigravity_user_settings_path()
    assert not settings_path.exists()


def test_provision_pre_trust_preserves_existing_settings(
    antigravity_agent: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Pre-trust must be additive: prior keys and entries stay verbatim."""
    settings_path = get_antigravity_user_settings_path()
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"trustedWorkspaces": ["/prior/workspace"], "colorScheme": "dark"}, indent=2))

    antigravity_agent.provision(
        host=antigravity_agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=antigravity_agent.mngr_ctx,
    )

    settings = _read_user_settings(isolated_home)
    assert "/prior/workspace" in settings["trustedWorkspaces"]
    assert str(antigravity_agent.work_dir) in settings["trustedWorkspaces"]
    assert settings["colorScheme"] == "dark"


def test_provision_pre_trust_is_idempotent(
    antigravity_agent: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Two provisioning passes produce a single trust entry, not duplicates."""
    options = CreateAgentOptions(agent_type=AgentTypeName("antigravity"))
    antigravity_agent.provision(host=antigravity_agent.host, options=options, mngr_ctx=antigravity_agent.mngr_ctx)
    antigravity_agent.provision(host=antigravity_agent.host, options=options, mngr_ctx=antigravity_agent.mngr_ctx)

    settings = _read_user_settings(isolated_home)
    trusted = settings["trustedWorkspaces"]
    assert trusted.count(str(antigravity_agent.work_dir)) == 1


def _provision(agent: AntigravityAgent) -> None:
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )


@pytest.fixture
def antigravity_agent_without_common_transcript(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    return _make_antigravity_agent(local_provider, tmp_path, AntigravityAgentConfig(emit_common_transcript=False))


def test_provision_writes_raw_transcript_streamer(antigravity_agent: AntigravityAgent, isolated_home: Path) -> None:
    """The raw streamer is required by HasTranscriptMixin and is provisioned unconditionally."""
    _provision(antigravity_agent)
    expected = antigravity_agent._get_agent_dir() / "commands" / "stream_transcript.sh"
    assert expected.exists()
    body = expected.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "antigravity_transcript/events.jsonl" in body
    assert expected.stat().st_mode & 0o111


def test_provision_writes_raw_streamer_even_when_common_transcript_disabled(
    antigravity_agent_without_common_transcript: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Raw capture is required regardless of the common-transcript flag."""
    _provision(antigravity_agent_without_common_transcript)
    expected = antigravity_agent_without_common_transcript._get_agent_dir() / "commands" / "stream_transcript.sh"
    assert expected.exists()


def test_provision_with_common_transcript_writes_converter(
    antigravity_agent: AntigravityAgent, isolated_home: Path
) -> None:
    """`emit_common_transcript=True` (default) provisions common_transcript.sh."""
    _provision(antigravity_agent)
    expected = antigravity_agent._get_agent_dir() / "commands" / "common_transcript.sh"
    assert expected.exists()
    body = expected.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "events/antigravity/common_transcript/events.jsonl" in body
    assert expected.stat().st_mode & 0o111


def test_provision_without_common_transcript_omits_converter(
    antigravity_agent_without_common_transcript: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Disabling emit_common_transcript suppresses the converter script."""
    _provision(antigravity_agent_without_common_transcript)
    expected = antigravity_agent_without_common_transcript._get_agent_dir() / "commands" / "common_transcript.sh"
    assert not expected.exists()


def test_provision_writes_background_tasks_supervisor(
    antigravity_agent: AntigravityAgent, isolated_home: Path
) -> None:
    """The supervisor is the single backgrounded entry point launched from assemble_command."""
    _provision(antigravity_agent)
    expected = antigravity_agent._get_agent_dir() / "commands" / "antigravity_background_tasks.sh"
    assert expected.exists()
    body = expected.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "stream_transcript.sh" in body
    assert "common_transcript.sh" in body
    assert expected.stat().st_mode & 0o111


def test_provision_writes_supervisor_even_when_common_transcript_disabled(
    antigravity_agent_without_common_transcript: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """The supervisor is unconditional; the converter check inside it is the gate."""
    _provision(antigravity_agent_without_common_transcript)
    expected = (
        antigravity_agent_without_common_transcript._get_agent_dir() / "commands" / "antigravity_background_tasks.sh"
    )
    assert expected.exists()
