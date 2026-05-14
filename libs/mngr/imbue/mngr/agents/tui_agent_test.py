"""Unit tests for InteractiveTuiAgent."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pydantic
import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_agent import _check_paste_content
from imbue.mngr.agents.tui_agent import _normalize_for_match
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import SendMessageError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


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


def test_probe_subclass_uses_submission_signal_by_default() -> None:
    assert InteractiveTuiAgent.uses_submission_signal(_ProbeTuiAgent.model_construct()) is True


# =========================================================================
# Poll-based no-submission-signal Enter path
# =========================================================================


class _PollingProbeAgent(InteractiveTuiAgent[AgentTypeConfig]):
    """InteractiveTuiAgent subclass that captures send-keys calls and synthesizes pane content.

    Overrides only the two methods the poll-based no-signal Enter path touches:
    ``_send_enter_keystroke`` (records the shell command instead of running it)
    and ``_capture_pane_content`` (returns text containing the cleared indicator
    unless ``always_missing_indicator`` is set, which simulates a swallowed Enter).
    """

    TUI_READY_INDICATOR = "probe-banner"
    TUI_INPUT_CLEARED_INDICATOR = "probe-cleared"
    captured_commands: list[str] = pydantic.Field(default_factory=list)
    pane_capture_count: int = pydantic.Field(default=0)
    always_missing_indicator: bool = pydantic.Field(default=False)

    def uses_submission_signal(self) -> bool:
        return False

    def _send_enter_keystroke(self, tmux_target: str) -> None:
        self.captured_commands.append(f"tmux send-keys -t '{tmux_target}' Enter")

    def _capture_pane_content(self, tmux_target: str, include_scrollback: bool = False) -> str | None:
        self.pane_capture_count += 1
        if self.always_missing_indicator:
            return "user typed message but Enter was swallowed"
        return "input row cleared -- probe-cleared visible"


class _PollingProbeAgentNoClearedIndicator(_PollingProbeAgent):
    """Variant with TUI_INPUT_CLEARED_INDICATOR=None: exercises the fire-and-forget fallback."""

    TUI_INPUT_CLEARED_INDICATOR = None


def _make_polling_probe(probe_class: type[_PollingProbeAgent]) -> _PollingProbeAgent:
    """Construct a polling probe via model_construct (no real host needed for these tests)."""
    return probe_class.model_construct(
        id=AgentId.generate(),
        name=AgentName("polling-probe"),
        agent_type=AgentTypeName("probe"),
    )


def test_send_enter_and_poll_for_input_ready_returns_when_indicator_appears() -> None:
    """When TUI_INPUT_CLEARED_INDICATOR is set and visible, the path returns on the first Enter."""
    agent = _make_polling_probe(_PollingProbeAgent)
    agent._send_enter_and_poll_for_input_ready("probe-target")

    assert agent.captured_commands == ["tmux send-keys -t 'probe-target' Enter"]
    assert agent.pane_capture_count >= 1


@pytest.mark.allow_warnings
def test_send_enter_and_poll_for_input_ready_retries_when_indicator_missing() -> None:
    """If the cleared indicator never reappears, the Enter keystroke is retried before raising.

    Marked allow_warnings because the final timeout path intentionally logs a
    captured pane snapshot via logger.error before raising SendMessageError.
    """
    agent = _make_polling_probe(_PollingProbeAgent)
    agent.always_missing_indicator = True

    with pytest.raises(SendMessageError, match="Timeout waiting for TUI input prompt to clear"):
        agent._send_enter_and_poll_for_input_ready("probe-target")
    assert agent.captured_commands == ["tmux send-keys -t 'probe-target' Enter"] * 3


def test_send_enter_and_poll_for_input_ready_falls_back_when_no_cleared_indicator() -> None:
    """When TUI_INPUT_CLEARED_INDICATOR is None, the path sends a single Enter and does not poll."""
    agent = _make_polling_probe(_PollingProbeAgentNoClearedIndicator)
    agent._send_enter_and_poll_for_input_ready("probe-target")

    assert agent.captured_commands == ["tmux send-keys -t 'probe-target' Enter"]
    assert agent.pane_capture_count == 0


# =========================================================================
# Paste-detection helpers
# =========================================================================


def test_normalize_for_match_strips_non_alnum_and_lowercases() -> None:
    """_normalize_for_match should strip non-alphanumeric chars and lowercase."""
    assert _normalize_for_match("Hello, World!") == "helloworld"
    assert _normalize_for_match("foo-bar_baz 123") == "foobarbaz123"
    assert _normalize_for_match("") == ""
    assert _normalize_for_match("  \n\t  ") == ""


def test_check_paste_content_detects_paste_indicator() -> None:
    """_check_paste_content returns True when tmux paste indicator is present."""
    assert _check_paste_content("some text\n[Pasted text 123 chars]\nmore text", "anything") is True


def test_check_paste_content_detects_fuzzy_content_match() -> None:
    """_check_paste_content returns True when normalized message tail is found in pane."""
    pane = "prompt> hello world this is a test message"
    assert _check_paste_content(pane, "Hello, World! This is a test message") is True


def test_check_paste_content_returns_false_when_no_match() -> None:
    """_check_paste_content returns False when neither paste indicator nor content match."""
    pane = "prompt> totally different content"
    assert _check_paste_content(pane, "Hello, World! This is a test message") is False


def test_check_paste_content_handles_empty_message() -> None:
    """_check_paste_content returns True for empty messages (nothing to verify)."""
    assert _check_paste_content("some content", "") is True


def test_check_paste_content_short_message_tail() -> None:
    """_check_paste_content with a short message should use its full length as probe."""
    pane = "prompt> abc"
    assert _check_paste_content(pane, "abc") is True


def test_check_paste_content_long_message_uses_tail() -> None:
    """_check_paste_content with a long message should match on the last 60 chars."""
    tail = "a" * 60
    message = "x" * 100 + tail
    pane = "prompt> " + tail
    assert _check_paste_content(pane, message) is True


# =========================================================================
# Signal-based Enter path (real tmux)
# =========================================================================


@pytest.fixture
def signal_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> _ProbeTuiAgent:
    """Real-host probe used by @pytest.mark.tmux tests that drive tmux directly."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return _ProbeTuiAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("signal-probe"),
        agent_type=AgentTypeName("probe"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )


@pytest.mark.tmux
def test_send_enter_and_wait_for_signal_returns_true_when_signal_received(
    signal_agent: _ProbeTuiAgent,
) -> None:
    """Test that _send_enter_and_wait_for_signal returns True when tmux wait-for signal is received."""
    session_name = f"{signal_agent.mngr_ctx.config.prefix}{signal_agent.name}"
    tmux_target = f"{session_name}:0"
    wait_channel = f"mngr-submit-{session_name}"

    signal_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' 'bash'",
        timeout_seconds=5.0,
    )

    try:
        # Simulate what the UserPromptSubmit hook does: signal the channel
        # from a background process after a short delay.
        signal_agent.host.execute_idempotent_command(
            f"( sleep 0.1 && tmux wait-for -S '{wait_channel}' ) &",
            timeout_seconds=1.0,
        )

        assert signal_agent._send_enter_and_wait_for_signal(tmux_target, wait_channel) is True
    finally:
        signal_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null",
            timeout_seconds=5.0,
        )


@pytest.mark.tmux
def test_send_enter_and_wait_for_signal_returns_false_on_timeout(
    signal_agent: _ProbeTuiAgent,
) -> None:
    """Test that _send_enter_and_wait_for_signal returns False when signal times out."""
    signal_agent.enter_submission_timeout_seconds = 0.2
    session_name = f"{signal_agent.mngr_ctx.config.prefix}{signal_agent.name}"
    tmux_target = f"{session_name}:0"
    wait_channel = f"mngr-submit-never-signaled-{session_name}"

    signal_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' 'bash'",
        timeout_seconds=5.0,
    )

    try:
        assert signal_agent._send_enter_and_wait_for_signal(tmux_target, wait_channel) is False
    finally:
        signal_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null",
            timeout_seconds=5.0,
        )
