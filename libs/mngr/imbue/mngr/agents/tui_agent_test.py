"""Unit tests for InteractiveTuiAgent."""

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.config.data_types import AgentTypeConfig


class _ProbeTuiAgent(InteractiveTuiAgent[AgentTypeConfig]):
    TUI_READY_INDICATOR = "probe-banner"


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
