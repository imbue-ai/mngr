"""Unit tests for InteractiveTuiAgent's contract."""

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
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
