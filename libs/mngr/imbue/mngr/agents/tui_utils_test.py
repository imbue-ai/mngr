"""Unit tests for tui_utils."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pydantic
import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_utils import _check_paste_content
from imbue.mngr.agents.tui_utils import _normalize_for_match
from imbue.mngr.agents.tui_utils import send_enter_and_poll_for_cleared_indicator
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.agents.tui_utils import send_enter_keystroke
from imbue.mngr.agents.tui_utils import send_enter_via_tmux_wait_for_hook
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance

# =========================================================================
# Paste-detection helpers
# =========================================================================


def test_normalize_for_match_strips_non_alnum_and_lowercases() -> None:
    assert _normalize_for_match("Hello, World!") == "helloworld"
    assert _normalize_for_match("foo-bar_baz 123") == "foobarbaz123"
    assert _normalize_for_match("") == ""
    assert _normalize_for_match("  \n\t  ") == ""


def test_check_paste_content_detects_paste_indicator() -> None:
    assert _check_paste_content("some text\n[Pasted text 123 chars]\nmore text", "anything") is True


def test_check_paste_content_detects_fuzzy_content_match() -> None:
    pane = "prompt> hello world this is a test message"
    assert _check_paste_content(pane, "Hello, World! This is a test message") is True


def test_check_paste_content_returns_false_when_no_match() -> None:
    pane = "prompt> totally different content"
    assert _check_paste_content(pane, "Hello, World! This is a test message") is False


def test_check_paste_content_handles_empty_message() -> None:
    assert _check_paste_content("some content", "") is True


def test_check_paste_content_short_message_tail() -> None:
    """A short message should use its full length as the probe."""
    assert _check_paste_content("prompt> abc", "abc") is True


def test_check_paste_content_long_message_uses_tail() -> None:
    """A long message should match on the last 60 chars."""
    tail = "a" * 60
    message = "x" * 100 + tail
    pane = "prompt> " + tail
    assert _check_paste_content(pane, message) is True


# =========================================================================
# Send-Enter strategies via in-memory probe agent
# =========================================================================


class _ProbeAgent(BaseAgent[AgentTypeConfig]):
    """In-memory BaseAgent that captures host commands and synthesizes pane content.

    Overrides only what the strategy helpers touch via the agent: the host's
    ``execute_stateful_command`` (replaced by a recording stub) and the
    private ``_capture_pane_content`` / ``_check_pane_contains`` methods.
    """

    captured_commands: list[str] = pydantic.Field(default_factory=list)
    pane_capture_count: int = pydantic.Field(default=0)
    always_missing_indicator: bool = pydantic.Field(default=False)

    def _capture_pane_content(self, tmux_target: TmuxWindowTarget, include_scrollback: bool = False) -> str | None:
        self.pane_capture_count += 1
        if self.always_missing_indicator:
            return "user typed message but Enter was swallowed"
        return "input row cleared -- probe-cleared visible"

    def _check_pane_contains(self, tmux_target: TmuxWindowTarget, text: str) -> bool:
        content = self._capture_pane_content(tmux_target)
        return content is not None and text in content


class _RecorderHost(pydantic.BaseModel):
    """In-memory host stub: records each command and returns a configurable result."""

    captured: list[str] = pydantic.Field(default_factory=list)
    succeed: bool = True

    def execute_stateful_command(self, command: str, **_: object) -> CommandResult:
        self.captured.append(command)
        if self.succeed:
            return CommandResult(stdout="", stderr="", success=True)
        return CommandResult(stdout="", stderr="boom", success=False)


def _make_probe(*, command_succeeds: bool = True, always_missing_indicator: bool = False) -> _ProbeAgent:
    host = _RecorderHost(succeed=command_succeeds)
    return _ProbeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("probe"),
        agent_type=AgentTypeName("probe"),
        host=host,
        captured_commands=host.captured,
        always_missing_indicator=always_missing_indicator,
    )


def test_send_enter_keystroke_runs_tmux_send_keys() -> None:
    agent = _make_probe()
    send_enter_keystroke(agent, TmuxWindowTarget(session_name="probe-target", window=0))
    assert agent.captured_commands == ["tmux send-keys -t =probe-target:0 Enter"]


def test_send_enter_keystroke_raises_on_command_failure() -> None:
    agent = _make_probe(command_succeeds=False)
    with pytest.raises(SendMessageError, match="tmux send-keys Enter failed"):
        send_enter_keystroke(agent, TmuxWindowTarget(session_name="probe-target", window=0))


def test_send_enter_best_effort_sends_single_keystroke() -> None:
    agent = _make_probe()
    send_enter_best_effort(agent, TmuxWindowTarget(session_name="probe-target", window=0))
    assert agent.captured_commands == ["tmux send-keys -t =probe-target:0 Enter"]


def test_send_enter_and_poll_returns_when_indicator_appears() -> None:
    agent = _make_probe()
    send_enter_and_poll_for_cleared_indicator(
        agent, TmuxWindowTarget(session_name="probe-target", window=0), cleared_indicator="probe-cleared"
    )
    assert agent.captured_commands == ["tmux send-keys -t =probe-target:0 Enter"]
    assert agent.pane_capture_count >= 1


@pytest.mark.allow_warnings
def test_send_enter_and_poll_retries_when_indicator_missing() -> None:
    """If the indicator never reappears, retry the keystroke before raising.

    Marked allow_warnings because the final timeout path intentionally logs a
    captured pane snapshot via logger.error before raising.
    """
    agent = _make_probe(always_missing_indicator=True)
    with pytest.raises(SendMessageError, match="Timeout waiting for TUI input prompt to clear"):
        send_enter_and_poll_for_cleared_indicator(
            agent,
            TmuxWindowTarget(session_name="probe-target", window=0),
            cleared_indicator="probe-cleared",
            max_attempts=2,
            per_attempt_timeout_seconds=0.1,
        )
    assert agent.captured_commands == ["tmux send-keys -t =probe-target:0 Enter"] * 2


# =========================================================================
# Signal-hook strategy via real tmux
# =========================================================================


@pytest.fixture
def signal_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> _ProbeAgent:
    """Real-host probe used by @pytest.mark.tmux tests that drive tmux directly."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return _ProbeAgent.model_construct(
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
def test_send_enter_via_hook_returns_when_signal_received(signal_agent: _ProbeAgent) -> None:
    """The wait-for-hook strategy returns when the channel is signaled."""
    session_name = f"{signal_agent.mngr_ctx.config.prefix}{signal_agent.name}"
    tmux_target = TmuxWindowTarget(session_name=session_name, window=0)
    wait_channel = f"mngr-submit-{session_name}"

    signal_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' 'bash'",
        timeout_seconds=5.0,
    )

    try:
        # Simulate the UserPromptSubmit hook firing the wait-for after a short delay.
        signal_agent.host.execute_idempotent_command(
            f"( sleep 0.1 && tmux wait-for -S '{wait_channel}' ) &",
            timeout_seconds=1.0,
        )

        send_enter_via_tmux_wait_for_hook(
            signal_agent,
            tmux_target,
            wait_channel=wait_channel,
            timeout_seconds=2.0,
            accept_marker_command=None,
        )
    finally:
        signal_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null",
            timeout_seconds=5.0,
        )


@pytest.mark.tmux
@pytest.mark.allow_warnings
def test_send_enter_via_hook_raises_on_timeout(signal_agent: _ProbeAgent) -> None:
    """The wait-for-hook strategy raises SendMessageError on timeout."""
    session_name = f"{signal_agent.mngr_ctx.config.prefix}{signal_agent.name}"
    tmux_target = TmuxWindowTarget(session_name=session_name, window=0)
    wait_channel = f"mngr-submit-never-signaled-{session_name}"

    signal_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' 'bash'",
        timeout_seconds=5.0,
    )

    try:
        with pytest.raises(SendMessageError, match="Timeout waiting for message submission signal"):
            send_enter_via_tmux_wait_for_hook(
                signal_agent,
                tmux_target,
                wait_channel=wait_channel,
                timeout_seconds=0.2,
                accept_marker_command=None,
            )
    finally:
        signal_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null",
            timeout_seconds=5.0,
        )
