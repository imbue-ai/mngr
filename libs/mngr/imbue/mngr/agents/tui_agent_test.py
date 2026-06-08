"""Unit tests for InteractiveTuiAgent's contract."""

from types import SimpleNamespace

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import _send_enter_and_wait_for_signal
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.hosts.tmux import TmuxWindowTarget


class _ProbeTuiAgent(InteractiveTuiAgent[AgentTypeConfig]):
    TUI_READY_INDICATOR = "probe-banner"

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        send_enter_best_effort(self, tmux_target)


def test_interactive_tui_agent_subclasses_base_agent() -> None:
    assert issubclass(InteractiveTuiAgent, BaseAgent)


def test_probe_subclass_inherits_tui_ready_indicator_via_class_var() -> None:
    assert _ProbeTuiAgent.TUI_READY_INDICATOR == "probe-banner"


def test_probe_subclass_get_tui_ready_indicator_reads_class_var() -> None:
    """Without instantiation we can still assert the method body returns the class var."""
    indicator = InteractiveTuiAgent.get_tui_ready_indicator(_ProbeTuiAgent.model_construct())
    assert indicator == "probe-banner"


def test_send_enter_and_validate_is_abstract_on_interactive_tui_agent() -> None:
    """The send-enter-and-validate operation must be implemented by every subclass."""
    assert "_send_enter_and_validate" in InteractiveTuiAgent.__abstractmethods__
    # Subclasses that pick a strategy clear the abstractness.
    assert "_send_enter_and_validate" not in _ProbeTuiAgent.__abstractmethods__


def _fake_agent_capturing(commands: list[str], *, success: bool = True) -> SimpleNamespace:
    """A minimal agent whose host records each submission command and reports ``success``."""

    def execute_stateful_command(command: str, *args: object, **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(success=success, stdout="", stderr="")

    host = SimpleNamespace(
        build_source_env_prefix=lambda agent: "export MNGR_AGENT_STATE_DIR=/s &&",
        execute_stateful_command=execute_stateful_command,
    )
    return SimpleNamespace(name="probe", host=host)


_FAKE_TARGET = SimpleNamespace(as_shell_arg=lambda: "session:0.0")


def test_send_enter_waits_on_hook_only_without_a_queue_log() -> None:
    """With no enqueue log, a single command waits on the hook signal alone (original behavior)."""
    commands: list[str] = []
    agent = _fake_agent_capturing(commands)
    result = _send_enter_and_wait_for_signal(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        wait_channel="mngr-submit-x",
        timeout_seconds=1.0,
        queue_log_path_template=None,
    )
    assert result is True
    # Exactly one host round-trip, and it watches the hook but not the enqueue log.
    assert len(commands) == 1
    assert "tmux wait-for" in commands[0]
    assert "enqueue" not in commands[0]


def test_send_enter_watches_enqueue_and_hook_concurrently_with_a_queue_log() -> None:
    """With an enqueue log, the single command watches BOTH the enqueue event and the hook.

    This is the fast-path that lets a busy agent confirm on enqueue without
    blocking the full submission timeout on the (slow) hook.
    """
    commands: list[str] = []
    agent = _fake_agent_capturing(commands)
    result = _send_enter_and_wait_for_signal(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        wait_channel="mngr-submit-x",
        timeout_seconds=1.0,
        queue_log_path_template="$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl",
    )
    assert result is True
    # Still a single host round-trip (the two conditions are watched in one command)...
    assert len(commands) == 1
    # ...and it watches the hook AND a fresh enqueue in the transcript log.
    assert "tmux wait-for" in commands[0]
    assert "enqueue" in commands[0]
    assert "events.jsonl" in commands[0]


def test_send_enter_returns_false_when_the_command_fails() -> None:
    """A non-success result (timeout / no confirmation) surfaces as False."""
    commands: list[str] = []
    agent = _fake_agent_capturing(commands, success=False)
    result = _send_enter_and_wait_for_signal(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        wait_channel="mngr-submit-x",
        timeout_seconds=1.0,
        queue_log_path_template="$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl",
    )
    assert result is False
