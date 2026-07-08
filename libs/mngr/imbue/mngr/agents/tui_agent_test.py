"""Unit tests for InteractiveTuiAgent's contract."""

import contextlib
import re
from types import SimpleNamespace
from typing import Any
from typing import Final
from typing import Generator
from typing import cast

import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import _send_enter_and_wait_for_signal
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.agents.tui_utils import wait_for_tui_ready
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.primitives import AgentName


class _ProbeTuiAgent(InteractiveTuiAgent[AgentTypeConfig]):
    TUI_READY_INDICATOR = "probe-banner"

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        send_enter_best_effort(self, tmux_target)


class _RecordingTuiAgent(_ProbeTuiAgent):
    """Probe that records the order of ``send_message`` steps against stubbed pane content.

    ``pane_content`` is what every pane capture returns; setting it to a string
    that lacks ``TUI_READY_INDICATOR`` makes the readiness wait time out, while
    one that contains both the indicator and the message makes the whole pipeline
    succeed without touching a real host or tmux.
    """

    pane_content: str = ""
    steps: list[str] = []

    @property
    def tmux_target(self) -> TmuxWindowTarget:
        return TmuxWindowTarget(session_name="s", window=0)

    @contextlib.contextmanager
    def _message_lock(self) -> Generator[None, None, None]:
        yield

    def _capture_pane_content(self, tmux_target: TmuxWindowTarget, include_scrollback: bool = False) -> str | None:
        return self.pane_content

    def _preflight_send_message(self, tmux_target: TmuxWindowTarget) -> None:
        self.steps.append("preflight")

    def _send_tmux_literal_keys(self, tmux_target: TmuxWindowTarget, message: str) -> None:
        self.steps.append("paste")

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        self.steps.append("enter")


def _make_recording_agent(pane_content: str) -> _RecordingTuiAgent:
    return _RecordingTuiAgent.model_construct(name=AgentName("probe"), pane_content=pane_content, steps=[])


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


_RESUME_MESSAGE: Final[str] = "please resume the task"


def test_send_message_waits_for_ready_indicator_before_pasting() -> None:
    """send_message must confirm the TUI ready indicator before any keystrokes.

    The readiness wait happens inside send_message (not only on the create
    path), so it covers resume too: pasting before the indicator is visible
    would drop keystrokes into a still-replaying transcript.
    """
    pane = f"probe-banner {_RESUME_MESSAGE}"
    agent = _make_recording_agent(pane)
    agent.send_message(_RESUME_MESSAGE)
    # Readiness is gated before paste: the steps run in order only because
    # wait_for_tui_ready saw the indicator first.
    assert agent.steps == ["preflight", "paste", "enter"]


@pytest.mark.allow_warnings
def test_wait_for_tui_ready_raises_when_indicator_never_appears() -> None:
    """When the indicator never appears, the readiness wait raises instead of returning.

    ``send_message`` calls ``wait_for_tui_ready`` before pasting, so if the pane
    never shows the indicator (e.g. a transcript still replaying), this raise is
    what stops keystrokes from being dropped into a not-yet-ready session. A
    short timeout keeps the test off the production 30s default.
    """
    agent = _make_recording_agent(pane_content="restored conversation, no prompt yet")
    with pytest.raises(SendMessageError, match="Timeout waiting for TUI to be ready"):
        wait_for_tui_ready(agent, agent.tmux_target, agent.get_tui_ready_indicator(), timeout_seconds=0.2)


@pytest.mark.allow_warnings
def test_wait_for_tui_ready_string_indicator_matches_literally() -> None:
    """A plain ``str`` indicator is an exact substring -- regex metacharacters are literal.

    The matching mode is chosen by the indicator's type, never by its contents, so
    the string "a.b" matches the literal "a.b" but not "axb".
    """
    present = _make_recording_agent(pane_content="ready: a.b prompt")
    wait_for_tui_ready(present, present.tmux_target, "a.b", timeout_seconds=0.2)

    absent = _make_recording_agent(pane_content="ready: axb prompt")
    with pytest.raises(SendMessageError, match="Timeout waiting for TUI to be ready"):
        wait_for_tui_ready(absent, absent.tmux_target, "a.b", timeout_seconds=0.2)


def test_wait_for_tui_ready_pattern_indicator_matches_as_regex() -> None:
    """A compiled ``re.Pattern`` indicator is matched with ``re.search``."""
    agent = _make_recording_agent(pane_content="ready: axb prompt")
    wait_for_tui_ready(agent, agent.tmux_target, re.compile(r"a.b"), timeout_seconds=0.2)


def test_send_message_runs_preflight_before_readiness_wait() -> None:
    """A blocking preflight condition must surface immediately, not hang on readiness.

    A blocking dialog occupies the pane, so the ready indicator never appears
    while it is up. If the readiness wait ran before preflight, send_message
    would block for the full readiness timeout instead of raising. Preflight
    must run first: here the pane lacks the indicator, yet the preflight error
    is raised promptly (not a "Timeout waiting for TUI" readiness error).
    """

    class _PreflightRaisingAgent(_RecordingTuiAgent):
        def _preflight_send_message(self, tmux_target: TmuxWindowTarget) -> None:
            self.steps.append("preflight")
            raise SendMessageError(str(self.name), "blocking dialog")

    agent = _PreflightRaisingAgent.model_construct(name=AgentName("probe"), pane_content="", steps=[])
    with pytest.raises(SendMessageError, match="blocking dialog"):
        agent.send_message("hello")
    assert agent.steps == ["preflight"]
