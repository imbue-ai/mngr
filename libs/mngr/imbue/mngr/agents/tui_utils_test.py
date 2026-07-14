"""Unit tests for tui_utils."""

import shlex
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pydantic
import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.tui_utils import SubmissionEvidenceProbe
from imbue.mngr.agents.tui_utils import _check_paste_content
from imbue.mngr.agents.tui_utils import _normalize_for_match
from imbue.mngr.agents.tui_utils import _parse_confirmation_output
from imbue.mngr.agents.tui_utils import build_changed_token_probe
from imbue.mngr.agents.tui_utils import build_confirmation_command
from imbue.mngr.agents.tui_utils import build_file_mtime_token_command
from imbue.mngr.agents.tui_utils import build_normalized_message_probe
from imbue.mngr.agents.tui_utils import is_slash_command_message
from imbue.mngr.agents.tui_utils import raise_for_unconfirmed_submission
from imbue.mngr.agents.tui_utils import send_enter_keystroke
from imbue.mngr.agents.tui_utils import submit_message_and_confirm
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


def test_build_normalized_message_probe_uses_normalized_tail() -> None:
    assert build_normalized_message_probe("Hello, World!") == "helloworld"
    tail = "b" * 60
    assert build_normalized_message_probe("x" * 100 + tail) == tail
    assert build_normalized_message_probe("!!! ???") == ""


def test_is_slash_command_message() -> None:
    assert is_slash_command_message("/clear") is True
    assert is_slash_command_message("  /compact keep the summary short") is True
    assert is_slash_command_message("please run /clear for me") is False
    assert is_slash_command_message("hello") is False


# =========================================================================
# Probe constructors
# =========================================================================


def test_build_changed_token_probe_uses_same_command_for_baseline_and_poll() -> None:
    probe = build_changed_token_probe("marker", "cat /tmp/token 2>/dev/null")
    assert probe.name == "marker"
    assert probe.baseline_command == probe.poll_command == "cat /tmp/token 2>/dev/null"


def test_build_file_mtime_token_command_covers_gnu_and_bsd_stat() -> None:
    """The token command must work on both Linux (stat -c) and macOS (stat -f)."""
    command = build_file_mtime_token_command('"$MARKER"')
    assert 'stat -c %y "$MARKER"' in command
    assert 'stat -f %Fm "$MARKER"' in command


# =========================================================================
# Confirmation-command generation
# =========================================================================


_FAKE_TARGET = TmuxWindowTarget(session_name="probe-target", window=0)


def _make_probes() -> tuple[SubmissionEvidenceProbe, ...]:
    return (
        SubmissionEvidenceProbe(
            name="content probe",
            baseline_command="wc -c < /tmp/evidence 2>/dev/null",
            poll_command="tail -c +$(( ${base:-0} + 1 )) /tmp/evidence 2>/dev/null | grep -q -F needle && echo found",
        ),
        build_changed_token_probe("marker", "cat /tmp/marker-token 2>/dev/null"),
    )


def test_confirmation_command_sends_enter_after_baselines_and_before_polling() -> None:
    """The ordering invariant of the whole engine: baselines -> Enter -> poll loop.

    Confirmation may never be evaluated before Enter was sent (the historical
    stale-signal bug confirmed before Enter and then killed the pending
    keystroke), and baselines must be captured before Enter so this
    submission's evidence reads as a change.
    """
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET, probes=_make_probes(), normalized_pane_probe="needle", window_seconds=5
    )
    baseline_idx = command.index("base_0=")
    enter_idx = command.index("tmux send-keys -t =probe-target:0 Enter")
    loop_idx = command.index("while :")
    poll_idx = command.index("cur_0=")
    assert baseline_idx < enter_idx < loop_idx < poll_idx


def test_confirmation_command_has_no_background_jobs_or_traps() -> None:
    """The script must be strictly sequential.

    Background jobs plus an EXIT trap are what allowed the old implementation
    to kill the pending Enter keystroke after a spurious early confirmation.
    """
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET, probes=_make_probes(), normalized_pane_probe="needle", window_seconds=5
    )
    assert " & " not in command
    assert " &\n" not in command
    assert "trap" not in command
    assert "wait-for" not in command


def test_confirmation_command_embeds_probe_names_quoted() -> None:
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET, probes=_make_probes(), normalized_pane_probe="needle", window_seconds=5
    )
    assert "'content probe'" in command


def test_confirmation_command_gates_retries_on_pane_content() -> None:
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET, probes=_make_probes(), normalized_pane_probe="needle", window_seconds=5
    )
    assert "capture-pane" in command
    assert "*needle*" in command


def test_confirmation_command_retries_unconditionally_without_pane_probe() -> None:
    """A message that normalizes to nothing cannot be recognized in the pane."""
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET, probes=_make_probes(), normalized_pane_probe="", window_seconds=5
    )
    assert "capture-pane" not in command


# =========================================================================
# Output parsing
# =========================================================================


def test_parse_confirmation_output_reads_confirming_probe() -> None:
    outcome = _parse_confirmation_output("MNGR_CONFIRMED marker\n")
    assert outcome.is_confirmed is True
    assert outcome.confirming_probe_name == "marker"


def test_parse_confirmation_output_collects_retries_and_diagnostics() -> None:
    stdout = (
        "MNGR_ENTER_RETRY 3\n"
        "MNGR_ENTER_RETRY 10\n"
        "MNGR_UNCONFIRMED\n"
        "MNGR_PROBE content base=[123] final=[]\n"
        "unrelated noise\n"
    )
    outcome = _parse_confirmation_output(stdout)
    assert outcome.is_confirmed is False
    assert outcome.enter_retry_offsets == (3, 10)
    assert outcome.probe_diagnostics == ("content base=[123] final=[]",)


def test_parse_confirmation_output_handles_empty_output() -> None:
    outcome = _parse_confirmation_output("")
    assert outcome.is_confirmed is False
    assert outcome.confirming_probe_name is None


# =========================================================================
# submit_message_and_confirm via in-memory probe agent
# =========================================================================


class _RecorderHost(pydantic.BaseModel):
    """In-memory host stub: records each command and replays scripted results."""

    captured: list[str] = pydantic.Field(default_factory=list)
    scripted_results: list[CommandResult] = pydantic.Field(default_factory=list)

    def execute_stateful_command(self, command: str, **_: object) -> CommandResult:
        self.captured.append(command)
        if self.scripted_results:
            return self.scripted_results.pop(0)
        return CommandResult(stdout="", stderr="", success=True)


class _ProbeAgent(BaseAgent[AgentTypeConfig]):
    """In-memory BaseAgent that captures host commands and synthesizes pane content."""

    captured_commands: list[str] = pydantic.Field(default_factory=list)

    def _capture_pane_content(self, tmux_target: TmuxWindowTarget, include_scrollback: bool = False) -> str | None:
        return "pane still shows the typed message"


def _make_probe_agent(*scripted_results: CommandResult) -> _ProbeAgent:
    host = _RecorderHost(scripted_results=list(scripted_results))
    return _ProbeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("probe"),
        agent_type=AgentTypeName("probe"),
        host=host,
        captured_commands=host.captured,
    )


def test_send_enter_keystroke_runs_tmux_send_keys() -> None:
    agent = _make_probe_agent()
    send_enter_keystroke(agent, _FAKE_TARGET)
    assert agent.captured_commands == ["tmux send-keys -t =probe-target:0 Enter"]


def test_send_enter_keystroke_raises_on_command_failure() -> None:
    agent = _make_probe_agent(CommandResult(stdout="", stderr="boom", success=False))
    with pytest.raises(SendMessageError, match="tmux send-keys Enter failed"):
        send_enter_keystroke(agent, _FAKE_TARGET)


def test_submit_and_confirm_returns_confirmed_outcome() -> None:
    agent = _make_probe_agent(CommandResult(stdout="MNGR_CONFIRMED marker\n", stderr="", success=True))
    outcome = submit_message_and_confirm(
        agent=agent, tmux_target=_FAKE_TARGET, message="hello", probes=_make_probes(), timeout_seconds=5.0
    )
    assert outcome.is_confirmed is True
    assert outcome.confirming_probe_name == "marker"
    # One host round-trip for the whole confirmation window.
    assert len(agent.captured_commands) == 1


def test_submit_and_confirm_returns_unconfirmed_outcome_on_timeout() -> None:
    agent = _make_probe_agent(
        CommandResult(stdout="MNGR_UNCONFIRMED\nMNGR_PROBE marker base=[] final=[]\n", stderr="", success=False)
    )
    outcome = submit_message_and_confirm(
        agent=agent, tmux_target=_FAKE_TARGET, message="hello", probes=_make_probes(), timeout_seconds=5.0
    )
    assert outcome.is_confirmed is False
    assert outcome.probe_diagnostics == ("marker base=[] final=[]",)


@pytest.mark.allow_warnings
def test_submit_and_confirm_surfaces_abnormal_script_abort_in_diagnostics() -> None:
    """A script that dies without printing its own timeout marker is not a normal timeout.

    stdout carries neither MNGR_CONFIRMED nor MNGR_UNCONFIRMED, so the script
    aborted abnormally (e.g. a broken probe command crashed bash); the outcome
    must stay unconfirmed AND carry the script's stderr so the real error
    reaches the strict-failure diagnostics instead of being discarded.
    """
    agent = _make_probe_agent(
        CommandResult(stdout="", stderr="bash: syntax error near unexpected token", success=False)
    )
    outcome = submit_message_and_confirm(
        agent=agent, tmux_target=_FAKE_TARGET, message="hello", probes=_make_probes(), timeout_seconds=5.0
    )
    assert outcome.is_confirmed is False
    assert any("aborted abnormally" in diagnostic for diagnostic in outcome.probe_diagnostics)
    assert any("syntax error" in diagnostic for diagnostic in outcome.probe_diagnostics)


def test_submit_and_confirm_raises_when_enter_cannot_be_sent() -> None:
    agent = _make_probe_agent(CommandResult(stdout="MNGR_ENTER_FAILED\n", stderr="no such session", success=False))
    with pytest.raises(SendMessageError, match="tmux send-keys Enter failed"):
        submit_message_and_confirm(
            agent=agent, tmux_target=_FAKE_TARGET, message="hello", probes=_make_probes(), timeout_seconds=5.0
        )


def test_submit_and_confirm_degrades_to_plain_enter_without_probes() -> None:
    agent = _make_probe_agent()
    outcome = submit_message_and_confirm(
        agent=agent, tmux_target=_FAKE_TARGET, message="hello", probes=(), timeout_seconds=5.0
    )
    assert outcome.is_confirmed is False
    assert agent.captured_commands == ["tmux send-keys -t =probe-target:0 Enter"]


@pytest.mark.allow_warnings
def test_raise_for_unconfirmed_submission_includes_diagnostics() -> None:
    """The strict-failure error carries the probe diagnostics, retry history, and pane."""
    agent = _make_probe_agent(
        CommandResult(
            stdout="MNGR_ENTER_RETRY 3\nMNGR_UNCONFIRMED\nMNGR_PROBE marker base=[a] final=[a]\n",
            stderr="",
            success=False,
        )
    )
    outcome = submit_message_and_confirm(
        agent=agent, tmux_target=_FAKE_TARGET, message="hello", probes=_make_probes(), timeout_seconds=5.0
    )
    with pytest.raises(SendMessageError) as exc_info:
        raise_for_unconfirmed_submission(agent=agent, tmux_target=_FAKE_TARGET, outcome=outcome, timeout_seconds=5.0)
    error_text = str(exc_info.value)
    assert "marker base=[a] final=[a]" in error_text
    assert "[3]" in error_text
    assert "pane still shows the typed message" in error_text


# =========================================================================
# Real-tmux engine tests
# =========================================================================


@pytest.fixture
def tmux_probe_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> _ProbeAgent:
    """Real-host probe used by @pytest.mark.tmux tests that drive tmux directly."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return _ProbeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("engine-probe"),
        agent_type=AgentTypeName("probe"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )


@pytest.mark.tmux
def test_engine_confirms_when_enter_actually_executes(tmux_probe_agent: _ProbeAgent, tmp_path: Path) -> None:
    """Evidence appears exactly when Enter executes -> the engine confirms.

    The tmux session runs bash; the typed text is a command that writes the
    evidence file, so the changed-token probe can only confirm if Enter was
    genuinely delivered and consumed -- the engine's core promise.
    """
    session_name = f"{tmux_probe_agent.mngr_ctx.config.prefix}{tmux_probe_agent.name}"
    tmux_target = TmuxWindowTarget(session_name=session_name, window=0)
    evidence_path = tmp_path / "evidence-token"
    probe = build_changed_token_probe("evidence-file", f"cat '{evidence_path}' 2>/dev/null")

    tmux_probe_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' 'bash'", timeout_seconds=5.0
    )
    try:
        typed_command = f"echo token-36284 > '{evidence_path}'"
        tmux_probe_agent.host.execute_idempotent_command(
            f"tmux send-keys -t '={session_name}:0' -l -- {shlex.quote(typed_command)}",
            timeout_seconds=5.0,
        )
        outcome = submit_message_and_confirm(
            agent=tmux_probe_agent,
            tmux_target=tmux_target,
            message=typed_command,
            probes=(probe,),
            timeout_seconds=10.0,
        )
        assert outcome.is_confirmed is True
        assert outcome.confirming_probe_name == "evidence-file"
        assert evidence_path.read_text().strip() == "token-36284"
    finally:
        tmux_probe_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null", timeout_seconds=5.0
        )


@pytest.mark.tmux
def test_engine_reports_unconfirmed_when_no_evidence_appears(tmux_probe_agent: _ProbeAgent, tmp_path: Path) -> None:
    """With no evidence source ever changing, the engine reports unconfirmed after the window."""
    session_name = f"{tmux_probe_agent.mngr_ctx.config.prefix}{tmux_probe_agent.name}"
    tmux_target = TmuxWindowTarget(session_name=session_name, window=0)
    never_written = tmp_path / "never-written-token"
    probe = build_changed_token_probe("evidence-file", f"cat '{never_written}' 2>/dev/null")

    tmux_probe_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' 'bash'", timeout_seconds=5.0
    )
    try:
        outcome = submit_message_and_confirm(
            agent=tmux_probe_agent,
            tmux_target=tmux_target,
            message="text that is not in the pane 36284",
            probes=(probe,),
            timeout_seconds=1.0,
        )
        assert outcome.is_confirmed is False
    finally:
        tmux_probe_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null", timeout_seconds=5.0
        )
