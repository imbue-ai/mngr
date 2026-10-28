"""Unit tests for tui_utils."""

import shlex
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import cast

import pydantic
import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.base_agent import SEND_COMMAND_TIMEOUT_SECONDS
from imbue.mngr.agents.mock_host_test import ScriptedHost
from imbue.mngr.agents.mock_host_test import run_bash_against_stand_in_pane
from imbue.mngr.agents.tui_utils import EnterRetryGate
from imbue.mngr.agents.tui_utils import SubmissionConfirmationOutcome
from imbue.mngr.agents.tui_utils import SubmissionEvidenceProbe
from imbue.mngr.agents.tui_utils import _PANE_SHOWS_MESSAGE_FUNCTION
from imbue.mngr.agents.tui_utils import _PASTE_VISIBLE_TIMEOUT_SECONDS
from imbue.mngr.agents.tui_utils import _build_pane_message_probe
from imbue.mngr.agents.tui_utils import _build_pane_shows_message_function_lines
from imbue.mngr.agents.tui_utils import _build_retry_gate_check
from imbue.mngr.agents.tui_utils import _normalize_for_match
from imbue.mngr.agents.tui_utils import _parse_confirmation_output
from imbue.mngr.agents.tui_utils import build_changed_token_probe
from imbue.mngr.agents.tui_utils import build_confirmation_command
from imbue.mngr.agents.tui_utils import build_file_mtime_token_command
from imbue.mngr.agents.tui_utils import build_normalized_message_probe
from imbue.mngr.agents.tui_utils import build_unsubmitted_message_retry_gate
from imbue.mngr.agents.tui_utils import is_slash_command_message
from imbue.mngr.agents.tui_utils import raise_for_unconfirmed_submission
from imbue.mngr.agents.tui_utils import send_enter_keystroke
from imbue.mngr.agents.tui_utils import submit_message_and_confirm
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for

# The paste placeholder Claude Code and agy show, e.g. ``[Pasted text #2 +20 lines]``.
_PASTE_PLACEHOLDER_PREFIXES: Final[tuple[str, ...]] = ("[Pasted text ",)

# Paste-detection helpers


def test_normalize_for_match_strips_non_alnum_and_lowercases() -> None:
    assert _normalize_for_match("Hello, World!") == "helloworld"
    assert _normalize_for_match("foo-bar_baz 123") == "foobarbaz123"
    assert _normalize_for_match("") == ""
    assert _normalize_for_match("  \n\t  ") == ""


def test_build_pane_message_probe_keeps_lowercased_ascii_alnum_tail() -> None:
    assert _build_pane_message_probe("Hello, World! This is a test message") == "helloworldthisisatestmessage"
    assert _build_pane_message_probe("x" * 100 + "A" * 60) == "a" * 60
    assert _build_pane_message_probe("!!! ???") == ""


def test_build_pane_message_probe_drops_non_ascii_like_the_c_locale_tr_pipeline() -> None:
    """Python's lowercasing turns some non-ASCII letters into ASCII ones; the remote ``LC_ALL=C tr`` drops them."""
    assert _build_pane_message_probe("\u0130stanbul caf\u00e9 \u212a") == "stanbulcaf"


def _does_pane_show_message(raw_pane: str, message: str, paste_placeholder_prefixes: tuple[str, ...]) -> bool:
    """Run the script's pane check, the one the paste wait polls, against a stand-in pane showing ``raw_pane``."""
    function_lines = _build_pane_shows_message_function_lines(
        "=stand-in:0", _build_pane_message_probe(message), paste_placeholder_prefixes
    )
    script = "\n".join([*function_lines, _PANE_SHOWS_MESSAGE_FUNCTION])
    return run_bash_against_stand_in_pane(script, raw_pane).returncode == 0


def test_pane_check_counts_any_declared_placeholder_prefix_as_the_typed_message() -> None:
    assert _does_pane_show_message("❯ [Pasted: 5 lines]", "message 48151", ("[Pasted text ", "[Pasted: ")) is True


def test_pane_check_ignores_a_placeholder_the_tui_does_not_declare() -> None:
    assert _does_pane_show_message("> [Pasted text #1 +20 lines]", "message 48151", ("[Pasted: ",)) is False


def _is_retry_allowed_by_gate(gate: EnterRetryGate, raw_pane: str) -> bool:
    """Run the gate's condition the way the confirmation script does, against ``raw_pane``."""
    script = f"raw_pane={shlex.quote(raw_pane)}; {_build_retry_gate_check(gate)}"
    return subprocess.run(["bash", "-c", script], check=False, timeout=5.0).returncode == 0


def _unsubmitted_message_retry_gate(message: str) -> EnterRetryGate | None:
    """The gate ``build_unsubmitted_message_retry_gate`` builds for ``message`` with Claude Code's and agy's placeholder."""
    return build_unsubmitted_message_retry_gate(
        message=message, paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES
    )


_GATED_MESSAGE: Final[str] = "Please refactor the parser 62940"
_LONG_GATED_MESSAGE: Final[str] = "Summarise the release notes " * 6 + "and finish with the ticket number 80417"


@pytest.mark.parametrize(
    ("message", "raw_pane"),
    [
        pytest.param(
            "na\u00efve r\u00e9sum\u00e9 7", "> na\u00efve r\u00e9sum\u00e9 7", id="non_ascii_message_on_screen"
        ),
        pytest.param(_LONG_GATED_MESSAGE, f"> {_LONG_GATED_MESSAGE[-80:]}", id="only_the_long_message_tail_on_screen"),
        pytest.param(_LONG_GATED_MESSAGE, "> [Pasted text #1 +3 lines]", id="paste_placeholder"),
        pytest.param(_LONG_GATED_MESSAGE, "> ", id="nothing_pending"),
    ],
)
def test_unsubmitted_message_retry_gate_decides_like_the_paste_wait_pane_check(message: str, raw_pane: str) -> None:
    gate = _unsubmitted_message_retry_gate(message)
    assert gate is not None
    assert _is_retry_allowed_by_gate(gate, raw_pane) is _does_pane_show_message(
        raw_pane, message, _PASTE_PLACEHOLDER_PREFIXES
    )


@pytest.mark.parametrize(
    ("raw_pane", "is_allowed"),
    [
        pytest.param(f"> {_GATED_MESSAGE}", True, id="message_tail_on_screen"),
        pytest.param("> [Pasted text #1 +20 lines]", True, id="first_declared_placeholder"),
        pytest.param("❯ [Pasted: 5 lines]", True, id="second_declared_placeholder"),
        pytest.param("> ", False, id="nothing_pending"),
    ],
)
def test_unsubmitted_message_retry_gate_allows_a_retry_only_while_the_message_is_pending(
    raw_pane: str, is_allowed: bool
) -> None:
    gate = build_unsubmitted_message_retry_gate(
        message=_GATED_MESSAGE, paste_placeholder_prefixes=("[Pasted text ", "[Pasted: ")
    )
    assert gate is not None
    assert _is_retry_allowed_by_gate(gate, raw_pane) is is_allowed


def test_unsubmitted_message_retry_gate_without_placeholders_still_matches_the_message_tail() -> None:
    gate = build_unsubmitted_message_retry_gate(message=_GATED_MESSAGE, paste_placeholder_prefixes=())
    assert gate is not None
    assert _is_retry_allowed_by_gate(gate, f"> {_GATED_MESSAGE}") is True
    assert _is_retry_allowed_by_gate(gate, "> [Pasted text #1 +20 lines]") is False


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("!!! ???", id="punctuation_only"),
        # Python lowercases both letters to text holding an ASCII letter; the pane's C-locale normalization drops them.
        pytest.param("\u0130 \u212a", id="non_ascii_letters_only"),
    ],
)
def test_unsubmitted_message_retry_gate_is_absent_for_a_message_without_ascii_alphanumerics(message: str) -> None:
    assert _unsubmitted_message_retry_gate(message) is None


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


# Probe constructors


def test_build_changed_token_probe_uses_same_command_for_baseline_and_poll() -> None:
    probe = build_changed_token_probe("marker", "cat /tmp/token 2>/dev/null")
    assert probe.name == "marker"
    assert probe.baseline_command == probe.poll_command == "cat /tmp/token 2>/dev/null"


def test_build_file_mtime_token_command_covers_gnu_and_bsd_stat() -> None:
    """The token command must work on both Linux (stat -c) and macOS (stat -f)."""
    command = build_file_mtime_token_command('"$MARKER"')
    assert 'stat -c %y "$MARKER"' in command
    assert 'stat -f %Fm "$MARKER"' in command


# Confirmation-command generation


_FAKE_TARGET = TmuxWindowTarget(session_name="probe-target", window=0)
_NEEDLE_RETRY_GATE: Final[EnterRetryGate | None] = _unsubmitted_message_retry_gate("needle")


def _make_probes() -> tuple[SubmissionEvidenceProbe, ...]:
    return (
        SubmissionEvidenceProbe(
            name="content probe",
            baseline_command="wc -c < /tmp/evidence 2>/dev/null",
            poll_command="tail -c +$(( ${base:-0} + 1 )) /tmp/evidence 2>/dev/null | grep -q -F needle && echo found",
        ),
        build_changed_token_probe("marker", "cat /tmp/marker-token 2>/dev/null"),
    )


def _retry_loop_of(command: str) -> str:
    """The part of a confirmation command from its poll loop on, where the Enter retries live."""
    return command[command.index("while :") :]


def test_confirmation_command_sends_enter_after_paste_wait_and_baselines_and_before_polling() -> None:
    """The ordering invariant of the whole engine: paste wait -> baselines -> Enter -> poll loop.

    Confirmation may never be evaluated before Enter was sent (the historical
    stale-signal bug confirmed before Enter and then killed the pending
    keystroke), baselines must be captured before Enter so this submission's
    evidence reads as a change, and Enter must wait for the typed message to
    show, since a TUI can take an Enter that arrives too soon as a newline.
    """
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=_make_probes(),
        pane_message_probe="needle",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_NEEDLE_RETRY_GATE,
        window_seconds=5,
    )
    paste_wait_idx = command.index("until pane_shows_message")
    baseline_idx = command.index("base_0=")
    enter_idx = command.index("tmux send-keys -t =probe-target:0 Enter")
    loop_idx = command.index("while :")
    poll_idx = command.index("cur_0=")
    assert paste_wait_idx < baseline_idx < enter_idx < loop_idx < poll_idx


def test_confirmation_command_without_probes_stops_after_enter() -> None:
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=(),
        pane_message_probe="needle",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_NEEDLE_RETRY_GATE,
        window_seconds=5,
    )
    assert command.index("until pane_shows_message") < command.index("tmux send-keys -t =probe-target:0 Enter")
    assert "while :" not in command


def test_confirmation_command_has_no_background_jobs_or_traps() -> None:
    """The script must be strictly sequential.

    Background jobs plus an EXIT trap are what allowed the old implementation
    to kill the pending Enter keystroke after a spurious early confirmation.
    """
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=_make_probes(),
        pane_message_probe="needle",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_NEEDLE_RETRY_GATE,
        window_seconds=5,
    )
    assert " & " not in command
    assert " &\n" not in command
    assert "trap" not in command
    assert "wait-for" not in command


def test_confirmation_command_embeds_probe_names_quoted() -> None:
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=_make_probes(),
        pane_message_probe="needle",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_NEEDLE_RETRY_GATE,
        window_seconds=5,
    )
    assert "'content probe'" in command


def test_confirmation_command_gates_retries_on_pane_content() -> None:
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=_make_probes(),
        pane_message_probe="needle",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_NEEDLE_RETRY_GATE,
        window_seconds=5,
    )
    assert "capture-pane" in command
    assert "*needle*" in command


def test_confirmation_command_retries_unconditionally_without_pane_probe() -> None:
    """A message that normalizes to nothing cannot be recognized in the pane, so it is neither awaited nor gated on."""
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=_make_probes(),
        pane_message_probe="",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=None,
        window_seconds=5,
    )
    assert "capture-pane" not in command


def test_confirmation_command_runs_the_supplied_retry_gate_against_a_pane_capture() -> None:
    gate = EnterRetryGate(condition_command=NonEmptyStr("test -e /tmp/retry-gate-marker-73019"))
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=_make_probes(),
        pane_message_probe="needle",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=gate,
        window_seconds=5,
    )
    retry_loop = _retry_loop_of(command)
    assert "capture-pane" in retry_loop
    assert "test -e /tmp/retry-gate-marker-73019" in retry_loop
    assert _PANE_SHOWS_MESSAGE_FUNCTION not in retry_loop


def test_confirmation_command_stays_valid_bash_when_the_retry_gate_ends_in_a_comment() -> None:
    gate = EnterRetryGate(condition_command=NonEmptyStr("test -e /tmp/retry-gate-marker-50917\n# pending row shown"))
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=_make_probes(),
        pane_message_probe="needle",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=gate,
        window_seconds=5,
    )
    shell, flag, script = shlex.split(command)
    assert subprocess.run([shell, "-n", flag, script], check=False, timeout=5.0).returncode == 0


def test_retry_gate_check_discards_what_the_gate_prints() -> None:
    """A gate that prints the confirmation marker cannot put it on the confirmation script's stdout."""
    gate = EnterRetryGate(condition_command=NonEmptyStr("printf '%s\\n' MNGR_CONFIRMED forged-by-gate; exit 1"))
    script = f"raw_pane='> '; {_build_retry_gate_check(gate)}"
    result = subprocess.run(["bash", "-c", script], capture_output=True, check=False, timeout=5.0)
    assert result.stdout == b""


def test_confirmation_command_retries_unconditionally_without_a_retry_gate() -> None:
    """With no gate, Enter is re-sent at every offset without reading the pane."""
    command = build_confirmation_command(
        tmux_target=_FAKE_TARGET,
        probes=_make_probes(),
        pane_message_probe="needle",
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=None,
        window_seconds=5,
        retry_offsets=(4, 17),
    )
    retry_loop = _retry_loop_of(command)
    assert "capture-pane" not in retry_loop
    assert _PANE_SHOWS_MESSAGE_FUNCTION not in retry_loop
    assert retry_loop.count("tmux send-keys -t =probe-target:0 Enter 2>/dev/null") == 2


def test_enter_retry_gate_rejects_a_blank_condition() -> None:
    with pytest.raises(pydantic.ValidationError):
        EnterRetryGate.model_validate({"condition_command": "   "})


# Output parsing


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


# submit_message_and_confirm via in-memory probe agent


class _ProbeAgent(BaseAgent[AgentTypeConfig]):
    """In-memory BaseAgent that captures host commands and synthesizes pane content."""

    captured_commands: list[str] = pydantic.Field(default_factory=list)

    def _capture_pane_content(
        self, tmux_target: TmuxWindowTarget | str, include_scrollback: bool = False, include_styles: bool = False
    ) -> str | None:
        return "pane still shows the typed message"


def _make_probe_agent(
    *scripted_results: CommandResult,
    send_command_timeout_seconds: float = SEND_COMMAND_TIMEOUT_SECONDS,
    pane_id: str | None = None,
) -> _ProbeAgent:
    host = ScriptedHost(scripted_results=list(scripted_results), pane_id=pane_id)
    return _ProbeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("probe"),
        agent_type=AgentTypeName("probe"),
        host=host,
        captured_commands=host.captured,
        send_command_timeout_seconds=send_command_timeout_seconds,
    )


def test_send_enter_keystroke_runs_tmux_send_keys() -> None:
    agent = _make_probe_agent()
    send_enter_keystroke(agent, _FAKE_TARGET)
    assert cast(ScriptedHost, agent.host).sent_commands == ["tmux send-keys -t =probe-target:0 Enter"]


def test_send_enter_keystroke_bounds_every_tmux_command() -> None:
    agent = _make_probe_agent(send_command_timeout_seconds=7.5)
    host = cast(ScriptedHost, agent.host)
    send_enter_keystroke(agent, _FAKE_TARGET)
    assert len(host.captured) == 2
    assert host.captured_timeout_seconds == [7.5, 7.5]


def test_send_enter_keystroke_raises_on_command_failure() -> None:
    agent = _make_probe_agent(CommandResult(stdout="", stderr="boom", success=False))
    with pytest.raises(SendMessageError, match="tmux send-keys Enter failed"):
        send_enter_keystroke(agent, _FAKE_TARGET)


def test_submit_and_confirm_returns_confirmed_outcome() -> None:
    agent = _make_probe_agent(CommandResult(stdout="MNGR_CONFIRMED marker\n", stderr="", success=True))
    outcome = submit_message_and_confirm(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="hello",
        probes=_make_probes(),
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
        timeout_seconds=5.0,
    )
    assert outcome.is_confirmed is True
    assert outcome.confirming_probe_name == "marker"
    # One host round-trip for the whole confirmation window.
    assert len(cast(ScriptedHost, agent.host).sent_commands) == 1


def test_submit_and_confirm_flags_rejection_when_a_rejection_probe_confirms() -> None:
    probes = (
        *_make_probes(),
        SubmissionEvidenceProbe(
            name="unknown-command",
            baseline_command="wc -c < /tmp/evidence 2>/dev/null",
            poll_command="echo rejected",
            is_rejection=True,
        ),
    )
    agent = _make_probe_agent(CommandResult(stdout="MNGR_CONFIRMED unknown-command\n", stderr="", success=True))
    outcome = submit_message_and_confirm(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="/typo",
        probes=probes,
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_unsubmitted_message_retry_gate("/typo"),
        timeout_seconds=5.0,
    )
    assert outcome.is_confirmed is True
    assert outcome.is_rejection is True


def test_submit_and_confirm_does_not_flag_rejection_for_acceptance_probes() -> None:
    agent = _make_probe_agent(CommandResult(stdout="MNGR_CONFIRMED marker\n", stderr="", success=True))
    outcome = submit_message_and_confirm(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="hello",
        probes=_make_probes(),
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
        timeout_seconds=5.0,
    )
    assert outcome.is_confirmed is True
    assert outcome.is_rejection is False


def test_submit_and_confirm_returns_unconfirmed_outcome_on_timeout() -> None:
    agent = _make_probe_agent(
        CommandResult(stdout="MNGR_UNCONFIRMED\nMNGR_PROBE marker base=[] final=[]\n", stderr="", success=False)
    )
    outcome = submit_message_and_confirm(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="hello",
        probes=_make_probes(),
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
        timeout_seconds=5.0,
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
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="hello",
        probes=_make_probes(),
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
        timeout_seconds=5.0,
    )
    assert outcome.is_confirmed is False
    assert any("aborted abnormally" in diagnostic for diagnostic in outcome.probe_diagnostics)
    assert any("syntax error" in diagnostic for diagnostic in outcome.probe_diagnostics)


def test_submit_and_confirm_raises_when_enter_cannot_be_sent() -> None:
    agent = _make_probe_agent(CommandResult(stdout="MNGR_ENTER_FAILED\n", stderr="no such session", success=False))
    with pytest.raises(SendMessageError, match="tmux send-keys Enter failed"):
        submit_message_and_confirm(
            agent=agent,
            tmux_target=_FAKE_TARGET,
            message="hello",
            probes=_make_probes(),
            paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
            enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
            timeout_seconds=5.0,
        )


def test_submit_and_confirm_degrades_to_plain_enter_without_probes() -> None:
    agent = _make_probe_agent()
    outcome = submit_message_and_confirm(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="hello",
        probes=(),
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
        timeout_seconds=5.0,
    )
    assert outcome.is_confirmed is False
    sent_commands = cast(ScriptedHost, agent.host).sent_commands
    assert len(sent_commands) == 1
    assert "tmux send-keys -t =probe-target:0 Enter" in sent_commands[0]
    assert "while :" not in sent_commands[0]


def test_submit_and_confirm_without_probes_raises_when_the_submit_script_fails() -> None:
    """With nothing to confirm against, a failed script must not read as a best-effort Enter."""
    agent = _make_probe_agent(CommandResult(stdout="", stderr="ssh: connection reset", success=False))
    with pytest.raises(SendMessageError, match="submitting the message failed: ssh: connection reset"):
        submit_message_and_confirm(
            agent=agent,
            tmux_target=_FAKE_TARGET,
            message="hello",
            probes=(),
            paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
            enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
            timeout_seconds=5.0,
        )


def test_submit_and_confirm_without_probes_bounds_the_script_by_the_paste_wait_and_send_timeout() -> None:
    """With nothing to poll after Enter, the confirmation window must not stretch how long a wedged tmux holds the send."""
    agent = _make_probe_agent(send_command_timeout_seconds=7.5)
    host = cast(ScriptedHost, agent.host)
    submit_message_and_confirm(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="hello",
        probes=(),
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
        timeout_seconds=90.0,
    )
    assert host.captured_timeout_seconds[-1] == _PASTE_VISIBLE_TIMEOUT_SECONDS + 7.5


def test_submit_and_confirm_raises_when_the_typed_message_never_shows() -> None:
    agent = _make_probe_agent(CommandResult(stdout="MNGR_PASTE_NOT_VISIBLE\n", stderr="", success=False))
    with pytest.raises(SendMessageError, match="Timeout waiting for pasted content to appear"):
        submit_message_and_confirm(
            agent=agent,
            tmux_target=_FAKE_TARGET,
            message="hello",
            probes=_make_probes(),
            paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
            enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
            timeout_seconds=5.0,
        )


def test_submit_and_confirm_runs_the_retry_gate_against_the_agents_own_pane() -> None:
    agent = _make_probe_agent(CommandResult(stdout="MNGR_CONFIRMED marker\n", stderr="", success=True), pane_id="%41")
    submit_message_and_confirm(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="needle",
        probes=_make_probes(),
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_NEEDLE_RETRY_GATE,
        timeout_seconds=5.0,
    )
    (command,) = cast(ScriptedHost, agent.host).sent_commands
    assert "tmux capture-pane -p -t %41" in _retry_loop_of(command)
    assert "=probe-target:0" not in command


@pytest.mark.allow_warnings
def test_raise_for_unconfirmed_submission_includes_diagnostics() -> None:
    """The strict-failure error carries the probe diagnostics, retry history, and pane, classified as unconfirmed."""
    agent = _make_probe_agent(
        CommandResult(
            stdout="MNGR_ENTER_RETRY 3\nMNGR_UNCONFIRMED\nMNGR_PROBE marker base=[a] final=[a]\n",
            stderr="",
            success=False,
        )
    )
    outcome = submit_message_and_confirm(
        agent=agent,
        tmux_target=_FAKE_TARGET,
        message="hello",
        probes=_make_probes(),
        paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
        enter_retry_gate=_unsubmitted_message_retry_gate("hello"),
        timeout_seconds=5.0,
    )
    with pytest.raises(SendMessageError) as exc_info:
        raise_for_unconfirmed_submission(agent=agent, tmux_target=_FAKE_TARGET, outcome=outcome, timeout_seconds=5.0)
    error_text = str(exc_info.value)
    assert "marker base=[a] final=[a]" in error_text
    assert "[3]" in error_text
    assert "pane still shows the typed message" in error_text
    assert exc_info.value.kind == SendFailureKind.SUBMISSION_UNCONFIRMED


# Real-tmux engine tests


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
            paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
            enter_retry_gate=_unsubmitted_message_retry_gate(typed_command),
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
def test_engine_recognizes_mixed_case_punctuated_non_ascii_text_in_the_pane(
    tmux_probe_agent: _ProbeAgent, tmp_path: Path
) -> None:
    """The script's pane check and the Python-side probe normalize the same text the same way.

    The pane only writes the evidence file once it reads a line, so the send confirms only if the
    script recognized the message on screen and went on to press Enter.
    """
    session_name = f"{tmux_probe_agent.mngr_ctx.config.prefix}{tmux_probe_agent.name}"
    evidence_path = tmp_path / "evidence-token"
    message = "Fix the Café's README.md, then run `make TEST=1` -- ÅNGSTRÖM 36284!"
    pane_script = (
        f"printf '%s\\n' {shlex.quote(message)}; read -r _; "
        f"echo submitted > {shlex.quote(str(evidence_path))}; sleep 60"
    )
    tmux_probe_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -x 200 -y 24 {shlex.quote(f'sh -c {shlex.quote(pane_script)}')}",
        timeout_seconds=5.0,
    )
    try:
        outcome = submit_message_and_confirm(
            agent=tmux_probe_agent,
            tmux_target=TmuxWindowTarget(session_name=session_name, window=0),
            message=message,
            probes=(build_changed_token_probe("evidence-file", f"cat '{evidence_path}' 2>/dev/null"),),
            paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
            enter_retry_gate=_unsubmitted_message_retry_gate(message),
            timeout_seconds=10.0,
        )
        assert outcome.is_confirmed is True
        assert outcome.enter_retry_offsets == ()
    finally:
        tmux_probe_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null", timeout_seconds=5.0
        )


_SWALLOWED_MESSAGE: Final[str] = "a long brief for the agent 36284"
_SWALLOWED_MESSAGE_RETRY_GATE: Final[EnterRetryGate | None] = _unsubmitted_message_retry_gate(_SWALLOWED_MESSAGE)
# A gate keyed on a footer the way a queueing TUI would need, deaf to the message text.
_QUEUE_FOOTER_RETRY_GATE: Final[EnterRetryGate] = EnterRetryGate(
    condition_command=NonEmptyStr('case "$raw_pane" in *"Enter:queue 51873"*) exit 0 ;; esac; exit 1')
)


def _submit_to_enter_swallowing_pane(
    agent: _ProbeAgent,
    tmp_path: Path,
    screen: str,
    screen_after_first_enter: str,
    enter_retry_gate: EnterRetryGate | None,
    timeout_seconds: float,
) -> SubmissionConfirmationOutcome:
    """Submit ``_SWALLOWED_MESSAGE`` to a pane that shows ``screen`` and swallows the first Enter.

    The first Enter replaces the screen with ``screen_after_first_enter``. The pane writes the evidence file
    only on a second Enter, so the submission confirms if and only if the engine retried Enter under
    ``enter_retry_gate``.
    """
    session_name = f"{agent.mngr_ctx.config.prefix}{agent.name}"
    evidence_path = tmp_path / "evidence-token"
    pane_script = (
        f"printf '%s\\n' {shlex.quote(screen)}; read -r _; "
        f"printf '\\033[2J\\033[H%s\\n' {shlex.quote(screen_after_first_enter)}; read -r _; "
        f"echo retried > {shlex.quote(str(evidence_path))}; sleep 60"
    )
    agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -x 200 -y 24 {shlex.quote(f'sh -c {shlex.quote(pane_script)}')}",
        timeout_seconds=5.0,
    )
    try:
        return submit_message_and_confirm(
            agent=agent,
            tmux_target=TmuxWindowTarget(session_name=session_name, window=0),
            message=_SWALLOWED_MESSAGE,
            probes=(build_changed_token_probe("evidence-file", f"cat '{evidence_path}' 2>/dev/null"),),
            paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
            enter_retry_gate=enter_retry_gate,
            timeout_seconds=timeout_seconds,
        )
    finally:
        agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null", timeout_seconds=5.0
        )


@pytest.mark.tmux
@pytest.mark.parametrize(
    "screen",
    [
        # Claude Code and agy show a long pasted message as a placeholder, so its text is never on screen.
        pytest.param("> [Pasted text #1 +20 lines]", id="paste_placeholder"),
        pytest.param(f"> {_SWALLOWED_MESSAGE}", id="message_text"),
    ],
)
def test_engine_retries_enter_while_the_message_is_pending(
    tmux_probe_agent: _ProbeAgent, tmp_path: Path, screen: str
) -> None:
    """A swallowed Enter is retried while the input box still holds the unsubmitted message."""
    outcome = _submit_to_enter_swallowing_pane(
        tmux_probe_agent,
        tmp_path,
        screen,
        screen_after_first_enter=screen,
        enter_retry_gate=_SWALLOWED_MESSAGE_RETRY_GATE,
        timeout_seconds=10.0,
    )
    assert outcome.is_confirmed is True
    assert len(outcome.enter_retry_offsets) == 1


@pytest.mark.tmux
def test_engine_withholds_the_enter_retry_when_nothing_is_pending(
    tmux_probe_agent: _ProbeAgent, tmp_path: Path
) -> None:
    """Once the message and any paste placeholder are gone from the screen, Enter is not re-sent."""
    outcome = _submit_to_enter_swallowing_pane(
        tmux_probe_agent,
        tmp_path,
        f"> {_SWALLOWED_MESSAGE}",
        screen_after_first_enter="> ",
        enter_retry_gate=_SWALLOWED_MESSAGE_RETRY_GATE,
        timeout_seconds=5.0,
    )
    assert outcome.is_confirmed is False
    assert outcome.enter_retry_offsets == ()


@pytest.mark.tmux
@pytest.mark.parametrize(
    ("screen", "is_retried"),
    [
        pytest.param(f"> {_SWALLOWED_MESSAGE}\nEnter:queue 51873", True, id="gate_allows"),
        pytest.param(f"#1 {_SWALLOWED_MESSAGE}\nEnter:send now", False, id="gate_refuses_despite_message_text"),
    ],
)
def test_engine_follows_a_custom_retry_gate_instead_of_the_message_text(
    tmux_probe_agent: _ProbeAgent, tmp_path: Path, screen: str, is_retried: bool
) -> None:
    """A supplied gate alone decides the retry, even while the message text is on screen."""
    outcome = _submit_to_enter_swallowing_pane(
        tmux_probe_agent,
        tmp_path,
        screen,
        screen_after_first_enter=screen,
        enter_retry_gate=_QUEUE_FOOTER_RETRY_GATE,
        timeout_seconds=5.0,
    )
    assert outcome.is_confirmed is is_retried
    assert len(outcome.enter_retry_offsets) == (1 if is_retried else 0)


@pytest.mark.tmux
def test_engine_reports_unconfirmed_when_no_evidence_appears(tmux_probe_agent: _ProbeAgent, tmp_path: Path) -> None:
    """With no evidence source ever changing, the engine reports unconfirmed after the window."""
    session_name = f"{tmux_probe_agent.mngr_ctx.config.prefix}{tmux_probe_agent.name}"
    tmux_target = TmuxWindowTarget(session_name=session_name, window=0)
    never_written = tmp_path / "never-written-token"
    probe = build_changed_token_probe("evidence-file", f"cat '{never_written}' 2>/dev/null")

    message = "echo nothing writes the evidence 36284"
    tmux_probe_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' 'bash'", timeout_seconds=5.0
    )
    try:
        tmux_probe_agent.host.execute_idempotent_command(
            f"tmux send-keys -t '={session_name}:0' -l -- {shlex.quote(message)}", timeout_seconds=5.0
        )
        outcome = submit_message_and_confirm(
            agent=tmux_probe_agent,
            tmux_target=tmux_target,
            message=message,
            probes=(probe,),
            paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
            enter_retry_gate=_unsubmitted_message_retry_gate(message),
            timeout_seconds=1.0,
        )
        assert outcome.is_confirmed is False
    finally:
        tmux_probe_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null", timeout_seconds=5.0
        )


@pytest.mark.tmux
def test_engine_gives_up_without_enter_when_the_typed_message_never_shows(
    tmux_probe_agent: _ProbeAgent, tmp_path: Path
) -> None:
    """A message that never shows in the pane is never submitted: the script exits before Enter.

    The pane records the first line it reads. A line typed after the script returns is that first
    line only if the script sent no Enter of its own, since an Enter would have ended an empty one.
    """
    session_name = f"{tmux_probe_agent.mngr_ctx.config.prefix}{tmux_probe_agent.name}"
    target = TmuxWindowTarget(session_name=session_name, window=0)
    first_line_path = tmp_path / "first-line"
    pending_first_line_path = shlex.quote(str(tmp_path / "first-line.pending"))
    pane_script = (
        f"read -r line; printf '%s' \"$line\" > {pending_first_line_path}; "
        f"mv {pending_first_line_path} {shlex.quote(str(first_line_path))}; sleep 60"
    )
    sentinel_line = "typed after the script 36284"
    tmux_probe_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' {shlex.quote(f'sh -c {shlex.quote(pane_script)}')}",
        timeout_seconds=5.0,
    )
    try:
        command = build_confirmation_command(
            tmux_target=target,
            probes=(build_changed_token_probe("evidence-file", f"cat '{tmp_path / 'never'}' 2>/dev/null"),),
            pane_message_probe="textthatisnotinthepane36284",
            paste_placeholder_prefixes=_PASTE_PLACEHOLDER_PREFIXES,
            enter_retry_gate=_unsubmitted_message_retry_gate("textthatisnotinthepane36284"),
            window_seconds=1,
            paste_wait_seconds=1,
        )
        result = tmux_probe_agent.host.execute_stateful_command(command, timeout_seconds=10.0)
        assert "MNGR_PASTE_NOT_VISIBLE" in result.stdout

        tmux_probe_agent.host.execute_idempotent_command(
            f"tmux send-keys -t {target.as_shell_arg()} -l -- {shlex.quote(sentinel_line)} && "
            f"tmux send-keys -t {target.as_shell_arg()} Enter",
            timeout_seconds=5.0,
        )
        wait_for(first_line_path.exists, error_message="Expected the pane to read the line typed after the script")
        assert first_line_path.read_text() == sentinel_line
    finally:
        tmux_probe_agent.host.execute_idempotent_command(
            f"tmux kill-session -t '={session_name}' 2>/dev/null", timeout_seconds=5.0
        )
