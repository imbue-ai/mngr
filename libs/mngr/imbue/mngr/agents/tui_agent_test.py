"""Unit tests for InteractiveTuiAgent's contract."""

from types import SimpleNamespace
from typing import Any
from typing import Final
from typing import cast

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


def _fake_agent_capturing(commands: list[str], *, success: bool = True) -> BaseAgent[Any]:
    """A minimal agent whose host records each submission command and reports ``success``.

    Returned as ``BaseAgent[Any]`` via ``cast``: ``_send_enter_and_wait_for_signal``
    only ever touches ``agent.name`` and ``agent.host``, so a duck-typed namespace
    is sufficient at runtime while keeping the call sites type-correct.
    """

    def execute_stateful_command(command: str, *args: object, **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(success=success, stdout="", stderr="")

    host = SimpleNamespace(
        build_source_env_prefix=lambda agent: "export MNGR_AGENT_STATE_DIR=/s &&",
        execute_stateful_command=execute_stateful_command,
    )
    return cast(BaseAgent[Any], SimpleNamespace(name="probe", host=host))


_FAKE_TARGET = TmuxWindowTarget(session_name="session", window=0)


_PROBE_MARKER_COMMAND: Final[str] = "cat /s/marker.jsonl 2>/dev/null | grep accept-marker-probe | tail -n 1"


def test_send_enter_waits_on_hook_only_without_a_marker_command() -> None:
    """With no acceptance-marker command, a single command waits on the hook signal alone."""
    commands: list[str] = []
    agent = _fake_agent_capturing(commands)
    result = _send_enter_and_wait_for_signal(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        wait_channel="mngr-submit-x",
        timeout_seconds=1.0,
        accept_marker_command=None,
    )
    assert result is True
    # Exactly one host round-trip, and it waits on the hook with no marker probe
    # (the signal-only path uses no sentinel file).
    assert len(commands) == 1
    assert "tmux wait-for" in commands[0]
    assert "mktemp" not in commands[0]


def test_send_enter_watches_marker_and_hook_concurrently_with_a_marker_command() -> None:
    """With a marker command, the single command watches BOTH the marker and the hook.

    This is the fast-path that lets a busy agent confirm on the acceptance
    marker without blocking the full submission timeout on the (slow) hook. The
    agent-supplied probe is embedded verbatim so the module stays agent-neutral.
    """
    commands: list[str] = []
    agent = _fake_agent_capturing(commands)
    result = _send_enter_and_wait_for_signal(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        wait_channel="mngr-submit-x",
        timeout_seconds=1.0,
        accept_marker_command=_PROBE_MARKER_COMMAND,
    )
    assert result is True
    # Still a single host round-trip (the two conditions are watched in one command)...
    assert len(commands) == 1
    # ...and it watches the hook AND the agent-supplied acceptance-marker probe.
    assert "tmux wait-for" in commands[0]
    assert "accept-marker-probe" in commands[0]
    assert "mktemp" in commands[0]


def test_send_enter_returns_false_when_the_command_fails() -> None:
    """A non-success result (timeout / no confirmation) surfaces as False."""
    commands: list[str] = []
    agent = _fake_agent_capturing(commands, success=False)
    result = _send_enter_and_wait_for_signal(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        wait_channel="mngr-submit-x",
        timeout_seconds=1.0,
        accept_marker_command=_PROBE_MARKER_COMMAND,
    )
    assert result is False
