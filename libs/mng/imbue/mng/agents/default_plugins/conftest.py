import pytest

from imbue.mng.agents.default_plugins.headless_claude_agent import HeadlessClaude
from imbue.mng.primitives import AgentLifecycleState


@pytest.fixture
def headless_agent_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch HeadlessClaude.get_lifecycle_state to always return STOPPED.

    Use this in stream_output tests so the tailing loop terminates immediately
    instead of polling forever.
    """
    monkeypatch.setattr(HeadlessClaude, "get_lifecycle_state", lambda self: AgentLifecycleState.STOPPED)
