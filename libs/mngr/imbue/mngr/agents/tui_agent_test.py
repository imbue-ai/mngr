"""Unit tests for InteractiveTuiAgent's send/ready pipeline.

These exercise the real ``send_message`` and ``wait_for_ready_signal`` bodies
against an in-memory probe agent (no real tmux): the host is a recording stub
and pane content is synthesized so the paste-visibility and TUI-ready polls
resolve immediately. The probe records the observable effects -- which host
commands were issued, that ``_send_enter_and_validate`` ran after the literal
keys were sent, and that the ready indicator was polled on creation.
"""

from types import SimpleNamespace
from typing import Any
from typing import Final
from typing import cast

import pydantic

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import _send_enter_and_wait_for_signal
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName

_READY_INDICATOR = "probe-banner"


class _RecorderHost(pydantic.BaseModel):
    """In-memory host stub that records each command and reports success.

    ``is_local`` is False so ``_message_lock`` short-circuits without touching
    the filesystem (the lock path machinery is exercised elsewhere).
    """

    is_local: bool = False
    captured: list[str] = pydantic.Field(default_factory=list)

    def execute_stateful_command(self, command: str, **_: object) -> CommandResult:
        self.captured.append(command)
        return CommandResult(stdout="", stderr="", success=True)


class _ProbeTuiAgent(InteractiveTuiAgent[AgentTypeConfig]):
    """In-memory InteractiveTuiAgent that drives the real send/ready pipeline.

    Synthesizes pane content so ``wait_for_paste_visible`` and
    ``wait_for_tui_ready`` resolve on the first poll, and records that
    ``_send_enter_and_validate`` ran (capturing the host commands seen at that
    point so the test can assert ordering relative to the literal-keys send).
    """

    TUI_READY_INDICATOR = _READY_INDICATOR

    # Aliases the recorder host's command list (same list object, wired up in
    # _make_probe) so the test can read recorded commands through a typed field
    # rather than reaching into host.captured, whose static type is the host
    # interface. Mirrors the _ProbeAgent/captured_commands pattern in tui_utils_test.
    captured_commands: list[str] = pydantic.Field(default_factory=list)
    last_pasted_message: str = pydantic.Field(default="")
    pane_check_texts: list[str] = pydantic.Field(default_factory=list)
    enter_validated_after_commands: list[str] | None = pydantic.Field(default=None)

    def _capture_pane_content(self, tmux_target: TmuxWindowTarget, include_scrollback: bool = False) -> str | None:
        # Always render both the ready banner and the most recent pasted text so
        # that both the paste-visibility poll and the TUI-ready poll pass.
        return f"{_READY_INDICATOR}\nprompt> {self.last_pasted_message}"

    def _check_pane_contains(self, tmux_target: TmuxWindowTarget, text: str) -> bool:
        self.pane_check_texts.append(text)
        content = self._capture_pane_content(tmux_target)
        return content is not None and text in content

    def _send_tmux_literal_keys(self, tmux_target: TmuxWindowTarget, message: str) -> None:
        # Record the message so the synthesized pane reflects the paste, then
        # delegate to the real implementation so the host command is recorded.
        self.last_pasted_message = message
        super()._send_tmux_literal_keys(tmux_target, message)

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        # Snapshot the host commands issued so far so the test can confirm this
        # ran after the literal keys were sent.
        self.enter_validated_after_commands = list(self.captured_commands)


def _make_probe(mngr_ctx: MngrContext) -> _ProbeTuiAgent:
    host = _RecorderHost()
    return _ProbeTuiAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("probe"),
        agent_type=AgentTypeName("probe"),
        mngr_ctx=mngr_ctx,
        host=host,
        captured_commands=host.captured,
        agent_config=AgentTypeConfig(),
    )


def test_send_message_pastes_text_then_runs_enter_validation(temp_mngr_ctx: MngrContext) -> None:
    """send_message sends literal keys, confirms the paste, then validates Enter.

    Asserts the observable effects of the real pipeline: the literal-keys host
    command was issued, and ``_send_enter_and_validate`` ran *after* it (its
    snapshot of issued commands includes the send-keys call).
    """
    agent = _make_probe(temp_mngr_ctx)
    target_arg = agent.tmux_target.as_shell_arg()

    agent.send_message("hello probe")

    expected_send_keys = f"tmux send-keys -t {target_arg} -l -- 'hello probe'"
    assert agent.captured_commands == [expected_send_keys]
    # _send_enter_and_validate ran, and the literal-keys send happened before it.
    assert agent.enter_validated_after_commands == [expected_send_keys]


def test_wait_for_ready_signal_runs_start_action_and_polls_indicator_on_creation(
    temp_mngr_ctx: MngrContext,
) -> None:
    """On creation, wait_for_ready_signal runs the start action then polls the banner."""
    agent = _make_probe(temp_mngr_ctx)
    start_action_calls: list[None] = []

    agent.wait_for_ready_signal(is_creating=True, start_action=lambda: start_action_calls.append(None))

    assert start_action_calls == [None]
    # wait_for_tui_ready polled the pane for the subclass's ready indicator.
    assert _READY_INDICATOR in agent.pane_check_texts


def test_wait_for_ready_signal_skips_indicator_poll_when_not_creating(temp_mngr_ctx: MngrContext) -> None:
    """When not creating, only the start action runs -- no TUI-ready poll."""
    agent = _make_probe(temp_mngr_ctx)
    start_action_calls: list[None] = []

    agent.wait_for_ready_signal(is_creating=False, start_action=lambda: start_action_calls.append(None))

    assert start_action_calls == [None]
    assert agent.pane_check_texts == []


def test_interactive_tui_agent_pins_subclass_contract() -> None:
    """Pins the subclass contract (not behavior): a concrete TUI agent must be a
    BaseAgent, must declare TUI_READY_INDICATOR, and must implement
    _send_enter_and_validate (which is abstract on the base)."""
    assert issubclass(InteractiveTuiAgent, BaseAgent)
    assert "_send_enter_and_validate" in InteractiveTuiAgent.__abstractmethods__
    assert "_send_enter_and_validate" not in _ProbeTuiAgent.__abstractmethods__
    assert _ProbeTuiAgent.TUI_READY_INDICATOR == _READY_INDICATOR


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
