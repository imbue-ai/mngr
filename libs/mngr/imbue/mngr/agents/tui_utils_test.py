"""Unit tests for tui_utils."""

import os
import signal
import subprocess
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pydantic
import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_utils import _PERL_TIMEOUT
from imbue.mngr.agents.tui_utils import _TIMEOUT_FUNCTION
from imbue.mngr.agents.tui_utils import _build_signal_only_command
from imbue.mngr.agents.tui_utils import _build_signal_or_marker_command
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


# === Submission command builders ===

_TARGET = TmuxWindowTarget(session_name="sess", window=0)


@pytest.mark.parametrize(
    "command",
    [
        _build_signal_only_command(2.0, "chan", _TARGET),
        _build_signal_or_marker_command(2.0, "chan", _TARGET, "printf ''"),
    ],
)
def test_submission_commands_never_require_a_timeout_binary(command: str) -> None:
    """Both builders run on the agent's host, which for a local agent is the user's machine.

    That host may be macOS, which ships no ``timeout``, so every use of it has to
    be guarded by a probe with a fallback behind it.
    """
    assert "command -v timeout" in command
    assert "Time::HiRes" in command


@pytest.mark.parametrize(
    "command",
    [
        _build_signal_only_command(2.0, "chan", _TARGET),
        _build_signal_or_marker_command(2.0, "chan", _TARGET, "printf ''"),
    ],
)
def test_submission_commands_wait_on_the_channel_and_send_to_the_target(command: str) -> None:
    """The channel reaches ``tmux wait-for`` and the target reaches ``tmux send-keys``, not vice versa."""
    assert "chan" in command
    assert _TARGET.as_shell_arg() in command
    channel_index = command.index("chan")
    target_index = command.index(_TARGET.as_shell_arg())
    assert channel_index < target_index


def _run_perl_timeout(arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the perl fallback directly, so the assertions hold on a host that also has ``timeout``."""
    return subprocess.run(["bash", "-c", f"{_PERL_TIMEOUT} {arguments}"], capture_output=True, text=True, timeout=60)


def _is_killed_by_the_alarm(result: subprocess.CompletedProcess[str]) -> bool:
    """SIGALRM reads as ``-SIGALRM`` when bash ``exec``-ed perl, and as ``128 + SIGALRM`` when a shell outlived it."""
    return result.returncode in (-signal.SIGALRM, 128 + signal.SIGALRM)


@pytest.mark.parametrize("seconds", [0.5, 1.0])
def test_perl_fallback_kills_a_command_that_outlives_its_deadline(seconds: float) -> None:
    """A sub-second deadline is the interesting case.

    The whole-second ``alarm`` would round it down to ``alarm(0)``, which cancels the
    timer outright, so ``sleep 30`` would run to completion and report success.
    """
    start_time = time.monotonic()
    result = _run_perl_timeout(f"{seconds} sleep 30")
    assert _is_killed_by_the_alarm(result)
    assert time.monotonic() - start_time < 10.0


def test_perl_fallback_passes_through_the_command_s_own_exit_status() -> None:
    """``exec`` replaces perl, so a command that finishes first reports its own status."""
    assert _run_perl_timeout('10 sh -c "exit 7"').returncode == 7


def test_perl_fallback_fails_when_the_command_cannot_be_exec_ed() -> None:
    """A failed ``exec`` returns to perl, which would otherwise run off the end and exit 0."""
    assert _run_perl_timeout("5 /nonexistent-binary-for-this-test").returncode == 127


@pytest.mark.parametrize("seconds", [0.5, 30.0])
def test_timeout_function_runs_the_command_under_whichever_deadline_the_host_has(seconds: float) -> None:
    """The exit status differs by branch (``timeout`` gives 124, perl 142), so only the outcome is asserted."""
    script = f"{_TIMEOUT_FUNCTION} _timeout {seconds} sh -c 'sleep 5; exit 0'"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    expired = seconds < 5
    assert (result.returncode != 0) is expired


@pytest.mark.tmux
def test_send_enter_via_hook_confirms_on_the_hook_when_the_marker_never_advances(
    signal_agent: _ProbeAgent,
) -> None:
    """With a marker command, a submission that records no marker still confirms via the hook.

    This is the shape of Claude's ``/clear`` and ``/compact``: the TUI fires the
    submit hook but never enqueues a model turn, so the acceptance marker never
    moves and only the hook can confirm.
    """
    session_name = f"{signal_agent.mngr_ctx.config.prefix}{signal_agent.name}"
    tmux_target = TmuxWindowTarget(session_name=session_name, window=0)
    wait_channel = f"mngr-submit-marker-never-moves-{session_name}"

    signal_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' 'bash'",
        timeout_seconds=5.0,
    )
    try:
        signal_agent.host.execute_idempotent_command(
            f"( sleep 0.1 && tmux wait-for -S '{wait_channel}' ) &",
            timeout_seconds=1.0,
        )
        send_enter_via_tmux_wait_for_hook(
            signal_agent,
            tmux_target,
            wait_channel=wait_channel,
            timeout_seconds=3.0,
            # A constant token never sorts after its own baseline, so the marker
            # can never be what confirms this submission.
            accept_marker_command="printf 'frozen-marker'",
        )
    finally:
        signal_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null",
            timeout_seconds=5.0,
        )


def _run_with_failing_tmux(command: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run a built submission command with a ``tmux`` on PATH that fails immediately."""
    shim_dir = tmp_path / "failing_tmux_bin"
    shim_dir.mkdir()
    tmux_shim = shim_dir / "tmux"
    tmux_shim.write_text("#!/bin/sh\nexit 1\n")
    tmux_shim.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    return subprocess.run(command, shell=True, capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.parametrize(
    "command",
    [
        _build_signal_only_command(0.5, "chan", _TARGET),
        _build_signal_or_marker_command(0.5, "chan", _TARGET, "printf 'frozen'"),
    ],
)
def test_submission_command_fails_when_tmux_itself_fails(command: str, tmp_path: Path) -> None:
    """A ``tmux wait-for`` that errors out (no server, dead session) is not a submission.

    A killed ``tmux wait-for`` exits 0, so the deadline is tracked separately; the
    wait status still has to be honored, or an unreachable tmux reports success.
    """
    result = _run_with_failing_tmux(command, tmp_path)
    assert result.returncode != 0, f"reported success though tmux failed: {result.stdout!r}"


def _run_built_command_against_real_tmux(
    command: str, *, fire_signal_after_seconds: float | None
) -> subprocess.CompletedProcess[str]:
    """Run a built submission command against the test's isolated tmux server.

    The autouse tmux-isolation fixture already points ``TMUX_TMPDIR`` at a private
    server, so the bare ``tmux`` calls here and in the script never reach a real
    one. Creates the send-keys target session, optionally fires the hook after a
    delay, and returns the finished process.
    """
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", _TARGET.session_name, "bash"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    firing: subprocess.Popen[bytes] | None = None
    if fire_signal_after_seconds is not None:
        firing = subprocess.Popen(f"sleep {fire_signal_after_seconds} && tmux wait-for -S chan", shell=True)
    try:
        return subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    finally:
        if firing is not None:
            firing.wait(timeout=10)
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={_TARGET.session_name}"],
            capture_output=True,
            timeout=10,
        )


@pytest.mark.tmux
def test_signal_only_script_exits_zero_when_the_hook_fires() -> None:
    """The generated script confirms (exit 0) once the hook fires its wait-for channel."""
    command = _build_signal_only_command(5.0, "chan", _TARGET)
    result = _run_built_command_against_real_tmux(command, fire_signal_after_seconds=0.3)
    assert result.returncode == 0, f"script did not confirm the fired hook: {result.stderr!r}"


@pytest.mark.tmux
def test_signal_only_script_exits_nonzero_at_the_deadline() -> None:
    """With no hook, the generated script fails once the sleep-then-kill deadline passes."""
    command = _build_signal_only_command(0.5, "chan", _TARGET)
    result = _run_built_command_against_real_tmux(command, fire_signal_after_seconds=None)
    assert result.returncode != 0, f"script reported success though no hook fired: {result.stdout!r}"
