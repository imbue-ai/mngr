"""Unit tests for InteractiveTuiAgent."""

import pytest
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentName


class _ProbeTuiAgent(InteractiveTuiAgent[AgentTypeConfig]):
    TUI_READY_INDICATOR = "probe-banner"


class _UnsignalledProbeTuiAgent(_ProbeTuiAgent):
    """Probe subclass that disables the wait-for submission signal.

    Mirrors GeminiAgent's behavior: paste-detection on, submission signal off.
    Used to exercise the override branch of ``_send_enter_and_wait`` that
    issues a raw ``tmux send-keys ... Enter`` instead of delegating to the
    parent's wait-for-channel logic.
    """

    def uses_submission_signal(self) -> bool:
        return False


class _RecordingFakeHost(MutableModel):
    """Captures ``execute_stateful_command`` calls without actually running anything.

    The override path of ``InteractiveTuiAgent._send_enter_and_wait`` touches
    only this one host method, so a stub with just this method is sufficient
    to cover both the success and failure shapes.
    """

    command_result: CommandResult = Field(
        default=CommandResult(stdout="", stderr="", success=True),
        description="Result returned for every captured execute_stateful_command call",
    )
    captured_commands: list[str] = Field(
        default_factory=list,
        description="Every command string passed to execute_stateful_command, in order",
    )

    def execute_stateful_command(self, command: str) -> CommandResult:
        self.captured_commands.append(command)
        return self.command_result


def test_interactive_tui_agent_subclasses_base_agent() -> None:
    assert issubclass(InteractiveTuiAgent, BaseAgent)


def test_probe_subclass_inherits_tui_ready_indicator_via_class_var() -> None:
    assert _ProbeTuiAgent.TUI_READY_INDICATOR == "probe-banner"


def test_probe_subclass_get_tui_ready_indicator_reads_class_var() -> None:
    """Without instantiation we can still assert the method body returns the class var."""
    indicator = InteractiveTuiAgent.get_tui_ready_indicator(_ProbeTuiAgent.model_construct())
    assert indicator == "probe-banner"


def test_probe_subclass_uses_paste_detection_send() -> None:
    assert InteractiveTuiAgent.uses_paste_detection_send(_ProbeTuiAgent.model_construct()) is True


def test_probe_subclass_uses_submission_signal_by_default() -> None:
    assert InteractiveTuiAgent.uses_submission_signal(_ProbeTuiAgent.model_construct()) is True


def test_send_enter_and_wait_with_no_submission_signal_runs_sleep_then_tmux_enter() -> None:
    """When uses_submission_signal() is False, the override must send a sleep+Enter via the host."""
    fake_host = _RecordingFakeHost()
    agent = _UnsignalledProbeTuiAgent.model_construct(
        name=AgentName("probe-agent"),
        host=fake_host,
    )

    agent._send_enter_and_wait("probe-target")

    assert fake_host.captured_commands == ["sleep 0.2 && tmux send-keys -t 'probe-target' Enter"]


def test_send_enter_and_wait_with_no_submission_signal_raises_on_failure() -> None:
    """A failing execute_stateful_command must surface as SendMessageError with the stderr payload."""
    fake_host = _RecordingFakeHost(
        command_result=CommandResult(stdout="", stderr="tmux: no server running", success=False),
    )
    agent = _UnsignalledProbeTuiAgent.model_construct(
        name=AgentName("probe-agent"),
        host=fake_host,
    )

    with pytest.raises(SendMessageError) as exc_info:
        agent._send_enter_and_wait("probe-target")
    assert "tmux: no server running" in str(exc_info.value)
