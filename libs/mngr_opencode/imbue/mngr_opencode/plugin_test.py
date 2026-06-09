"""Unit tests for OpenCodeAgentConfig and OpenCodeAgent."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import ClassVar

import pytest

from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr_opencode.opencode_config import get_opencode_auth_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.opencode_config import get_shared_opencode_auth_path
from imbue.mngr_opencode.plugin import OpenCodeAgent
from imbue.mngr_opencode.plugin import OpenCodeAgentConfig
from imbue.mngr_opencode.plugin import _is_paste_echoed
from imbue.mngr_opencode.plugin import register_agent_type


def test_opencode_agent_config_has_correct_defaults() -> None:
    config = OpenCodeAgentConfig()

    assert str(config.command) == "opencode"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.config_overrides == {}
    assert config.sync_global_config is True
    assert config.symlink_auth is True
    assert config.auto_allow_permissions is False
    assert config.emit_common_transcript is True


def test_opencode_agent_config_merge_with_replaces_cli_args_and_overrides() -> None:
    """Override fields win under the base assign-by-default merge semantics."""
    base = OpenCodeAgentConfig()
    override = OpenCodeAgentConfig(cli_args=("--verbose",), config_overrides={"model": "anthropic/claude-sonnet-4-5"})

    merged = base.merge_with(override)

    assert isinstance(merged, OpenCodeAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert merged.config_overrides == {"model": "anthropic/claude-sonnet-4-5"}
    assert str(merged.command) == "opencode"


def test_opencode_agent_config_merge_with_rejects_other_type() -> None:
    class _OtherConfig(OpenCodeAgentConfig):
        pass

    with pytest.raises(ConfigParseError):
        OpenCodeAgentConfig().merge_with(_OtherConfig())


def test_opencode_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(OpenCodeAgent, InteractiveTuiAgent)


def test_opencode_agent_advertises_tui_ready_indicator() -> None:
    """Ready indicator is a footer-hint substring shown only once the input row is drawn.

    Deliberately not the ASCII-art splash banner, which renders before the
    input prompt exists. Verified against the live opencode 1.16.2 TUI.
    """
    assert OpenCodeAgent.TUI_READY_INDICATOR == "ctrl+p commands"


def test_opencode_agent_reports_opencode_process_name() -> None:
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_expected_process_name() == "opencode"


def test_opencode_agent_implements_send_enter_and_validate() -> None:
    assert "_send_enter_and_validate" not in OpenCodeAgent.__abstractmethods__


def test_register_agent_type_returns_opencode_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "opencode"
    assert agent_class is OpenCodeAgent
    assert config_class is OpenCodeAgentConfig


def test_is_common_transcript_enabled_reflects_config() -> None:
    enabled = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    disabled = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig(emit_common_transcript=False))
    assert enabled.is_common_transcript_enabled is True
    assert disabled.is_common_transcript_enabled is False


def test_raw_transcript_has_no_commands_scripts_but_common_does() -> None:
    """Raw capture is in-process (the .ts plugin), so no commands/ raw script; common is a converter."""
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_raw_transcript_scripts() == {}
    common = agent.get_common_transcript_scripts()
    assert "opencode_common_transcript.sh" in common
    assert common["opencode_common_transcript.sh"].strip() != ""


def _make_opencode_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: OpenCodeAgentConfig,
) -> OpenCodeAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return OpenCodeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-opencode"),
        agent_type=AgentTypeName("opencode"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


@pytest.fixture
def opencode_agent(local_provider: LocalProviderInstance, tmp_path: Path) -> OpenCodeAgent:
    return _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())


@pytest.fixture
def opencode_agent_no_common(local_provider: LocalProviderInstance, tmp_path: Path) -> OpenCodeAgent:
    return _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(emit_common_transcript=False))


def test_assemble_command_injects_per_agent_config_and_data_env(opencode_agent: OpenCodeAgent) -> None:
    """OPENCODE_CONFIG_DIR + XDG_DATA_HOME are injected as an env prefix on the opencode process."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, (), command_override=None))
    config_dir = str(opencode_agent._get_opencode_config_dir())
    data_home = str(opencode_agent._get_opencode_data_home())
    assert f"env OPENCODE_CONFIG_DIR={config_dir} XDG_DATA_HOME={data_home}" in command
    # The env prefix sits immediately before the opencode command, not on the whole chain.
    assert command.index("XDG_DATA_HOME") < command.index(" opencode")


def test_assemble_command_resume_prelude_guards_continue_on_root_session_file(
    opencode_agent: OpenCodeAgent,
) -> None:
    """`--continue` is appended only when the plugin-written root-session file exists."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, (), command_override=None))
    root_file = str(opencode_agent._get_root_session_file_path())
    assert f"if [ -s {root_file} ]; then set -- --continue; fi" in command


def test_assemble_command_launches_background_supervisor_when_common_enabled(
    opencode_agent: OpenCodeAgent,
) -> None:
    command = str(opencode_agent.assemble_command(opencode_agent.host, (), command_override=None))
    assert "( bash $MNGR_AGENT_STATE_DIR/commands/opencode_background_tasks.sh" in command
    assert command.strip().startswith("( bash ")


def test_assemble_command_omits_supervisor_when_common_disabled(
    opencode_agent_no_common: OpenCodeAgent,
) -> None:
    command = str(opencode_agent_no_common.assemble_command(opencode_agent_no_common.host, (), command_override=None))
    assert "opencode_background_tasks.sh" not in command
    assert "env OPENCODE_CONFIG_DIR=" in command


def test_assemble_command_appends_user_agent_args(opencode_agent: OpenCodeAgent) -> None:
    command = str(opencode_agent.assemble_command(opencode_agent.host, ("run", "hello"), command_override=None))
    assert " opencode run hello " in command


def test_assemble_command_shell_quotes_agent_args_with_spaces_and_parens(opencode_agent: OpenCodeAgent) -> None:
    """A model name with spaces/parens is shell-quoted, not spliced in raw (bash would mis-parse `(`)."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, ("--model", "A B (C)"), command_override=None))
    assert "'A B (C)'" in command


def _provision(agent: OpenCodeAgent) -> None:
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("opencode")),
        mngr_ctx=agent.mngr_ctx,
    )


def test_provision_writes_per_agent_config_with_schema(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    config_path = get_opencode_config_file_path(opencode_agent._get_opencode_config_dir())
    assert config_path.exists()
    parsed = json.loads(config_path.read_text())
    assert parsed["$schema"] == "https://opencode.ai/config.json"


def test_provision_inherits_user_global_config_and_applies_overrides(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """sync_global_config seeds from the user's ~/.config/opencode/opencode.json; overrides win."""
    user_config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    user_config_path.write_text(json.dumps({"theme": "user-theme", "model": "old/model"}))
    agent = _make_opencode_agent(
        local_provider, tmp_path, OpenCodeAgentConfig(config_overrides={"model": "anthropic/claude-sonnet-4-5"})
    )
    _provision(agent)
    parsed = json.loads(get_opencode_config_file_path(agent._get_opencode_config_dir()).read_text())
    assert parsed["theme"] == "user-theme"
    assert parsed["model"] == "anthropic/claude-sonnet-4-5"


def test_provision_injects_wildcard_allow_when_auto_allow(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(auto_allow_permissions=True))
    _provision(agent)
    parsed = json.loads(get_opencode_config_file_path(agent._get_opencode_config_dir()).read_text())
    assert parsed["permission"] == {"*": "allow"}


def test_provision_installs_lifecycle_plugin(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    plugin_path = get_opencode_plugin_path(opencode_agent._get_opencode_config_dir())
    assert plugin_path.exists()
    assert "MngrLifecyclePlugin" in plugin_path.read_text()


def test_provision_symlinks_auth_to_shared_path_by_default(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    auth_path = get_opencode_auth_path_for_data_home(opencode_agent._get_opencode_data_home())
    assert auth_path.is_symlink()
    assert auth_path.readlink() == get_shared_opencode_auth_path(Path.home())


def test_provision_copies_auth_when_symlink_disabled(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    shared = get_shared_opencode_auth_path(Path.home())
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text('{"anthropic":{"type":"api","key":"seed"}}')
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(symlink_auth=False))
    _provision(agent)
    auth_path = get_opencode_auth_path_for_data_home(agent._get_opencode_data_home())
    assert auth_path.exists()
    assert not auth_path.is_symlink()
    assert json.loads(auth_path.read_text())["anthropic"]["type"] == "api"


def test_provision_installs_transcript_scripts(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    commands_dir = opencode_agent._get_agent_dir() / "commands"
    assert (commands_dir / "opencode_common_transcript.sh").exists()
    assert (commands_dir / "opencode_background_tasks.sh").exists()


def test_provision_omits_converter_when_common_disabled(opencode_agent_no_common: OpenCodeAgent) -> None:
    _provision(opencode_agent_no_common)
    commands_dir = opencode_agent_no_common._get_agent_dir() / "commands"
    assert not (commands_dir / "opencode_common_transcript.sh").exists()
    # The supervisor is still installed; it simply finds nothing to supervise.
    assert (commands_dir / "opencode_background_tasks.sh").exists()


def test_provision_does_not_write_into_work_dir(opencode_agent: OpenCodeAgent) -> None:
    """The plugin isolates everything under the agent state dir; the user's work_dir is untouched."""
    _provision(opencode_agent)
    assert not (opencode_agent.work_dir / "opencode.json").exists()
    assert not (opencode_agent.work_dir / ".opencode").exists()


def test_provision_skips_auth_copy_when_no_shared_auth(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """copy mode with no shared auth.json simply skips seeding (the agent runs OpenCode's login flow)."""
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(symlink_auth=False))
    _provision(agent)
    auth_path = get_opencode_auth_path_for_data_home(agent._get_opencode_data_home())
    assert not auth_path.exists()


# --- Paste echo detection + self-healing retry ---


_FAKE_TMUX_TARGET = TmuxWindowTarget(session_name="test-session", window=0)


class _CannedPaneAgent(OpenCodeAgent):
    """Test agent that returns a fixed pane capture, for `_is_paste_echoed` checks."""

    canned_pane_content: ClassVar[str | None] = None

    @property
    def tmux_target(self) -> TmuxWindowTarget:
        return _FAKE_TMUX_TARGET

    def _capture_pane_content(self, tmux_target: TmuxWindowTarget, include_scrollback: bool = False) -> str | None:
        return type(self).canned_pane_content


def _make_canned_agent(content: str | None) -> _CannedPaneAgent:
    agent = _CannedPaneAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("canned"),
        agent_type=AgentTypeName("opencode"),
        agent_config=OpenCodeAgentConfig(),
    )
    type(agent).canned_pane_content = content
    return agent


def test_is_paste_echoed_matches_normalized_tail() -> None:
    agent = _make_canned_agent("> Count slowly from 1 to 20, one per line\n")
    assert _is_paste_echoed(agent, "Count slowly from 1 to 20, one per line") is True


def test_is_paste_echoed_accepts_bracketed_paste_indicator() -> None:
    agent = _make_canned_agent("input box [Pasted text +5 lines]")
    assert _is_paste_echoed(agent, "a very long message that is not literally echoed in the pane") is True


def test_is_paste_echoed_false_when_absent() -> None:
    agent = _make_canned_agent("Ask anything...")
    assert _is_paste_echoed(agent, "this message never landed") is False


def test_is_paste_echoed_false_when_capture_unavailable() -> None:
    agent = _make_canned_agent(None)
    assert _is_paste_echoed(agent, "anything") is False


class _ScriptedSendAgent(OpenCodeAgent):
    """Test agent that simulates OpenCode dropping the first N pastes, then echoing.

    Records sends/clears and reports the pasted text as visible only once the
    configured number of drops has elapsed, so the retry loop can be exercised
    without a real TUI. ClassVar timeouts are tiny so dropped attempts don't
    block on real polling.
    """

    drops_before_echo: ClassVar[int] = 0
    sends: ClassVar[int] = 0
    clears: ClassVar[int] = 0
    last_message: ClassVar[str] = ""
    _MAX_PASTE_ATTEMPTS: ClassVar[int] = 3
    _PASTE_ECHO_TIMEOUT_SECONDS: ClassVar[float] = 0.3
    _PASTE_ECHO_POLL_INTERVAL_SECONDS: ClassVar[float] = 0.05

    @property
    def tmux_target(self) -> TmuxWindowTarget:
        return _FAKE_TMUX_TARGET

    def _send_tmux_literal_keys(self, tmux_target: TmuxWindowTarget, message: str) -> None:
        type(self).sends += 1
        type(self).last_message = message

    def _clear_input_line(self) -> None:
        type(self).clears += 1

    def _capture_pane_content(self, tmux_target: TmuxWindowTarget, include_scrollback: bool = False) -> str | None:
        if type(self).sends > type(self).drops_before_echo:
            return type(self).last_message
        return ""


def _make_scripted_agent(drops_before_echo: int) -> _ScriptedSendAgent:
    agent = _ScriptedSendAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("scripted"),
        agent_type=AgentTypeName("opencode"),
        agent_config=OpenCodeAgentConfig(),
    )
    type(agent).drops_before_echo = drops_before_echo
    type(agent).sends = 0
    type(agent).clears = 0
    type(agent).last_message = ""
    return agent


def test_paste_retry_lands_on_first_attempt_when_stable() -> None:
    agent = _make_scripted_agent(drops_before_echo=0)
    agent._paste_message_with_retry("hello there")
    assert type(agent).sends == 1
    assert type(agent).clears == 0


def test_paste_retry_clears_and_resends_after_a_drop() -> None:
    agent = _make_scripted_agent(drops_before_echo=1)
    agent._paste_message_with_retry("hello there")
    # One drop -> a second send, with exactly one clear in between.
    assert type(agent).sends == 2
    assert type(agent).clears == 1


def test_paste_retry_raises_after_exhausting_attempts() -> None:
    agent = _make_scripted_agent(drops_before_echo=99)
    with pytest.raises(SendMessageError):
        agent._paste_message_with_retry("never lands")
    # One send per attempt, and a clear before every retry.
    assert type(agent).sends == _ScriptedSendAgent._MAX_PASTE_ATTEMPTS
    assert type(agent).clears == _ScriptedSendAgent._MAX_PASTE_ATTEMPTS - 1


# Lifecycle states that mean "the tmux pane is up" -- the same set
# find.ensure_agent_started treats as already-started (deliberately excludes
# STOPPED, i.e. no pane, and DONE, i.e. a dead shell pane).
_PANE_IS_UP_STATES = frozenset(
    {
        AgentLifecycleState.RUNNING,
        AgentLifecycleState.WAITING,
        AgentLifecycleState.REPLACED,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
    }
)


@pytest.mark.tmux
def test_send_message_delivers_to_real_tmux_pane(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """End-to-end through real tmux: send_message pastes into a pane and clears cleanly.

    Uses a ``cat`` pane (terminal echo) as a stand-in for the OpenCode input box
    so the real ``_send_tmux_literal_keys`` / ``_is_paste_echoed`` /
    ``_clear_input_line`` paths run without needing the OpenCode binary.
    """
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    session_name = agent.session_name
    try:
        agent.host.execute_idempotent_command(
            f"tmux new-session -d -s '{session_name}' 'cat'",
            timeout_seconds=5.0,
        )
        # Wait for the pane to actually come up. Mirror the "agent is started"
        # set that find.ensure_agent_started uses, rather than a loose "not
        # STOPPED" (which would also accept DONE -- a dead shell pane). A bare
        # ``cat`` pane reports REPLACED (it is neither ``opencode`` nor a shell),
        # which is in this set.
        wait_for(
            lambda: agent.get_lifecycle_state() in _PANE_IS_UP_STATES,
            timeout=5.0,
            error_message="tmux session not ready",
        )
        agent.send_message("a unique paste probe phrase for the cat pane")
        assert agent._check_pane_contains(agent.tmux_target, "a unique paste probe phrase for the cat pane")
        # The clear path (kill-line) runs against the real pane without error.
        agent._clear_input_line()
    finally:
        cleanup_tmux_session(session_name)
