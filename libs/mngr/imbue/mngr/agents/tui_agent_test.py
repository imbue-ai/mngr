"""Unit tests for InteractiveTuiAgent's contract."""

import contextlib
import inspect
import re
import shlex
from typing import Final
from typing import Generator
from typing import Sequence
from typing import cast

import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.mock_host_test import ScriptedHost
from imbue.mngr.agents.mock_host_test import run_bash_against_stand_in_pane
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import EnterRetryGate
from imbue.mngr.agents.tui_utils import SubmissionConfirmationPolicy
from imbue.mngr.agents.tui_utils import SubmissionEvidenceProbe
from imbue.mngr.agents.tui_utils import _PANE_SHOWS_MESSAGE_FUNCTION
from imbue.mngr.agents.tui_utils import build_changed_token_probe
from imbue.mngr.agents.tui_utils import build_normalized_message_probe
from imbue.mngr.agents.tui_utils import build_unsubmitted_message_retry_gate
from imbue.mngr.agents.tui_utils import wait_for_tui_ready
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import InvalidPastePlaceholderPrefixesError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName


class _ProbeTuiAgent(InteractiveTuiAgent[AgentTypeConfig]):
    TUI_READY_INDICATOR = "probe-banner"
    PASTE_PLACEHOLDER_PREFIXES = ("[Pasted text ",)

    def _build_enter_retry_gate(self, message: str) -> EnterRetryGate | None:
        return build_unsubmitted_message_retry_gate(
            message=message, paste_placeholder_prefixes=self.PASTE_PLACEHOLDER_PREFIXES
        )

    def _build_submission_evidence_probes(
        self, message: str, policy: SubmissionConfirmationPolicy
    ) -> Sequence[SubmissionEvidenceProbe]:
        return []


class _RecordingTuiAgent(_ProbeTuiAgent):
    """Probe that records the order of ``send_message`` steps against stubbed pane content.

    ``pane_content`` is what every pane capture returns; setting it to a string
    that lacks ``TUI_READY_INDICATOR`` makes the readiness wait time out, while
    one that contains both the indicator and the message makes the whole pipeline
    succeed without touching a real tmux (host commands go to a ``ScriptedHost``).
    """

    pane_content: str = ""
    # What preflight reports it saw; None models a preflight that does not read the pane.
    preflight_pane_content: str | None = None
    steps: list[str] = []
    built_policies: list[SubmissionConfirmationPolicy] = []
    probes_to_return: list[SubmissionEvidenceProbe] = []
    capture_count: int = 0

    @property
    def tmux_target(self) -> TmuxWindowTarget:
        return TmuxWindowTarget(session_name="s", window=0)

    @contextlib.contextmanager
    def _message_lock(self, timeout_seconds: float | None = None) -> Generator[None, None, None]:
        yield

    def _capture_pane_content(
        self, tmux_target: TmuxWindowTarget | str, include_scrollback: bool = False, include_styles: bool = False
    ) -> str | None:
        self.capture_count += 1
        return self.pane_content

    def _preflight_send_message(self, tmux_target: TmuxWindowTarget | str) -> str | None:
        self.steps.append("preflight")
        return self.preflight_pane_content

    def _send_tmux_literal_keys(self, tmux_target: TmuxWindowTarget | str, message: str) -> str:
        self.steps.append("paste")
        return self._send_target_arg(tmux_target)

    def _build_submission_evidence_probes(
        self, message: str, policy: SubmissionConfirmationPolicy
    ) -> Sequence[SubmissionEvidenceProbe]:
        self.built_policies.append(policy)
        return list(self.probes_to_return)


_RESUME_MESSAGE: Final[str] = "please resume the task"


def _make_recording_agent(
    pane_content: str,
    *scripted_results: CommandResult,
    probes: Sequence[SubmissionEvidenceProbe] = (),
    agent_class: type[_RecordingTuiAgent] = _RecordingTuiAgent,
    preflight_pane_content: str | None = None,
    pane_id: str | None = None,
) -> _RecordingTuiAgent:
    host = ScriptedHost(scripted_results=list(scripted_results), pane_id=pane_id)
    return agent_class.model_construct(
        id=AgentId.generate(),
        name=AgentName("probe"),
        pane_content=pane_content,
        preflight_pane_content=preflight_pane_content,
        steps=[],
        built_policies=[],
        probes_to_return=list(probes),
        host=host,
    )


def _never_confirming_probe() -> SubmissionEvidenceProbe:
    return build_changed_token_probe("never", "cat /tmp/never-token-36284 2>/dev/null")


def test_interactive_tui_agent_subclasses_base_agent() -> None:
    assert issubclass(InteractiveTuiAgent, BaseAgent)


def test_probe_subclass_inherits_tui_ready_indicator_via_class_var() -> None:
    assert _ProbeTuiAgent.TUI_READY_INDICATOR == "probe-banner"


def test_probe_subclass_get_tui_ready_indicator_reads_class_var() -> None:
    """Without instantiation we can still assert the method body returns the class var."""
    indicator = InteractiveTuiAgent.get_tui_ready_indicator(_ProbeTuiAgent.model_construct())
    assert indicator == "probe-banner"


def test_build_submission_evidence_probes_is_abstract_on_interactive_tui_agent() -> None:
    """Every subclass must declare its durable submission evidence."""
    assert "_build_submission_evidence_probes" in InteractiveTuiAgent.__abstractmethods__
    assert "_build_submission_evidence_probes" not in _ProbeTuiAgent.__abstractmethods__


def test_build_enter_retry_gate_is_abstract_on_interactive_tui_agent() -> None:
    """Every subclass must say when a swallowed Enter may be re-sent."""
    assert "_build_enter_retry_gate" in InteractiveTuiAgent.__abstractmethods__
    assert "_build_enter_retry_gate" not in _ProbeTuiAgent.__abstractmethods__


def test_send_message_waits_for_ready_indicator_before_pasting() -> None:
    """send_message must confirm the TUI ready indicator before any keystrokes.

    The readiness wait happens inside send_message (not only on the create
    path), so it covers resume too: pasting before the indicator is visible
    would drop keystrokes into a still-replaying transcript. With no evidence
    probes the send degrades to one best-effort Enter, sent by the host script
    once the typed message shows.
    """
    pane = f"probe-banner {_RESUME_MESSAGE}"
    agent = _make_recording_agent(pane)
    agent.send_message(_RESUME_MESSAGE)
    assert agent.steps == ["preflight", "paste"]
    host_commands = cast(ScriptedHost, agent.host).sent_commands
    assert len(host_commands) == 1
    assert "tmux send-keys -t =s:0 Enter" in host_commands[0]
    assert "while :" not in host_commands[0]


def test_send_message_resolves_the_agent_pane_once_and_addresses_every_step_to_it() -> None:
    """One pane lookup serves the whole send; each lookup is a process spawn."""
    agent = _make_recording_agent(
        f"probe-banner {_RESUME_MESSAGE}",
        CommandResult(stdout="MNGR_CONFIRMED never\n", stderr="", success=True),
        probes=[_never_confirming_probe()],
        pane_id="%7",
    )
    agent.send_message(_RESUME_MESSAGE)
    host = cast(ScriptedHost, agent.host)
    assert [c for c in host.captured if c.startswith("tmux show-options")] == [host.captured[0]]
    submit_command = host.sent_commands[-1]
    assert "tmux send-keys -t %7 Enter" in submit_command
    assert "=s:0" not in submit_command


def test_send_message_reuses_the_pane_preflight_saw_when_it_shows_the_tui_ready() -> None:
    """A preflight reading that already shows the ready indicator spares the readiness and leftover-text captures."""
    pane = f"probe-banner {_RESUME_MESSAGE}"
    agent = _make_recording_agent(pane, preflight_pane_content=pane)
    agent.send_message(_RESUME_MESSAGE)
    assert agent.capture_count == 0


def test_send_message_waits_for_readiness_when_preflight_saw_no_ready_pane() -> None:
    """A preflight reading without the indicator is not trusted: the send polls and captures afresh."""
    agent = _make_recording_agent(f"probe-banner {_RESUME_MESSAGE}", preflight_pane_content="still painting")
    agent.send_message(_RESUME_MESSAGE)
    assert agent.capture_count == 2


def test_send_message_selects_strict_policy_for_normal_messages() -> None:
    agent = _make_recording_agent(f"probe-banner {_RESUME_MESSAGE}")
    agent.send_message(_RESUME_MESSAGE)
    assert agent.built_policies == [SubmissionConfirmationPolicy.STRICT]


def test_send_message_selects_relaxed_policy_for_slash_commands() -> None:
    agent = _make_recording_agent("probe-banner /clear")
    agent.send_message("/clear")
    assert agent.built_policies == [SubmissionConfirmationPolicy.RELAXED]


@pytest.mark.parametrize("blank_message", ["", "  \n\t "])
def test_send_message_refuses_blank_message_without_touching_the_pane(blank_message: str) -> None:
    """A blank message leaves no input to submit, so no evidence could ever confirm it.

    It must fail at once rather than paste nothing, press Enter on an empty input
    box, and poll out the whole confirmation window.
    """
    agent = _make_recording_agent("probe-banner", probes=[_never_confirming_probe()])
    with pytest.raises(SendMessageError, match="the message is empty"):
        agent.send_message(blank_message)
    assert agent.steps == []
    assert agent.built_policies == []
    assert cast(ScriptedHost, agent.host).sent_commands == []


@pytest.mark.allow_warnings
def test_send_message_raises_when_strict_confirmation_times_out() -> None:
    """A normal message with probes that never confirm must fail loudly."""
    agent = _make_recording_agent(
        f"probe-banner {_RESUME_MESSAGE}",
        CommandResult(stdout="MNGR_UNCONFIRMED\nMNGR_PROBE never base=[] final=[]\n", stderr="", success=False),
        probes=[_never_confirming_probe()],
    )
    with pytest.raises(SendMessageError, match="Timeout waiting for message submission evidence"):
        agent.send_message(_RESUME_MESSAGE)


@pytest.mark.allow_warnings
def test_send_message_warns_but_succeeds_for_unconfirmed_slash_command() -> None:
    """A slash command with no observable evidence exits successfully with a warning.

    The warning is accompanied by a structured agent event (the append command
    lands on the host) so soft failures stay auditable.
    """
    agent = _make_recording_agent(
        "probe-banner /clear",
        CommandResult(stdout="MNGR_UNCONFIRMED\n", stderr="", success=False),
        probes=[_never_confirming_probe()],
    )
    agent.send_message("/clear")
    event_commands = [command for command in cast(ScriptedHost, agent.host).captured if "events/messages" in command]
    assert len(event_commands) == 1
    assert "relaxed_send_unconfirmed" in event_commands[0]


def test_send_message_confirms_slash_command_without_warning_event() -> None:
    agent = _make_recording_agent(
        "probe-banner /clear",
        CommandResult(stdout="MNGR_CONFIRMED never\n", stderr="", success=True),
        probes=[_never_confirming_probe()],
    )
    agent.send_message("/clear")
    event_commands = [command for command in cast(ScriptedHost, agent.host).captured if "events/messages" in command]
    assert event_commands == []


@pytest.mark.allow_warnings
def test_send_message_warns_and_records_event_for_preexisting_input_text() -> None:
    """Leftover input-box text triggers a warning + agent event, and the send proceeds."""

    class _LeftoverDetectingAgent(_RecordingTuiAgent):
        def _detect_preexisting_input_text(self, pane_content: str) -> str | None:
            return "previously stranded message"

    agent = _make_recording_agent(f"probe-banner {_RESUME_MESSAGE}", agent_class=_LeftoverDetectingAgent)
    agent.send_message(_RESUME_MESSAGE)
    host = cast(ScriptedHost, agent.host)
    event_commands = [command for command in host.captured if "events/messages" in command]
    assert len(event_commands) == 1
    assert "preexisting_input_text" in event_commands[0]
    # The send still went through (best-effort Enter recorded after the event).
    assert any("send-keys -t =s:0 Enter" in command for command in host.captured)


def test_send_message_records_cleared_event_instead_of_warning_when_restored_command_is_cleared() -> None:
    """A leftover the subclass clears is recorded as cleared, not warned about, and the send proceeds."""

    class _RestoredCommandClearingAgent(_RecordingTuiAgent):
        def _detect_preexisting_input_text(self, pane_content: str) -> str | None:
            return "/restored-command-71935"

        def _clear_preexisting_input_text_if_restored_command(
            self, tmux_target: TmuxWindowTarget | str, leftover_text: str
        ) -> bool:
            self.steps.append(f"clear {leftover_text}")
            return True

    agent = _make_recording_agent(f"probe-banner {_RESUME_MESSAGE}", agent_class=_RestoredCommandClearingAgent)
    agent.send_message(_RESUME_MESSAGE)
    assert agent.steps == ["preflight", "clear /restored-command-71935", "paste"]
    host = cast(ScriptedHost, agent.host)
    event_commands = [command for command in host.captured if "events/messages" in command]
    assert len(event_commands) == 1
    assert "cleared_restored_input_text" in event_commands[0]
    assert "/restored-command-71935" in event_commands[0]
    assert any("send-keys -t =s:0 Enter" in command for command in host.captured)


class _ChipPastingTuiAgent(_RecordingTuiAgent):
    PASTE_PLACEHOLDER_PREFIXES = ("[Pasted: ",)


@pytest.mark.parametrize(
    ("agent_class", "pane_content"),
    [
        pytest.param(_RecordingTuiAgent, "probe-banner > [Pasted text #1 +20 lines]", id="pasted_text_placeholder"),
        pytest.param(_ChipPastingTuiAgent, "probe-banner > [Pasted: 3 lines]", id="declared_placeholder"),
    ],
)
def test_send_message_counts_a_paste_placeholder_as_the_visible_paste(
    agent_class: type[_RecordingTuiAgent], pane_content: str
) -> None:
    """The input box shows only the TUI's paste placeholder, never the message text, and the send still proceeds."""
    agent = _make_recording_agent(pane_content, agent_class=agent_class)
    agent.send_message(_RESUME_MESSAGE)
    assert agent.steps == ["preflight", "paste"]
    (submit_command,) = cast(ScriptedHost, agent.host).sent_commands
    _, _, submit_script = shlex.split(submit_command)
    result = run_bash_against_stand_in_pane(submit_script, pane_content)
    assert result.stdout.splitlines() == ["tmux send-keys -t =s:0 Enter"]


@pytest.mark.parametrize(
    "declared_prefixes",
    [
        pytest.param("[Pasted: ", id="bare_string_missing_tuple_comma"),
        pytest.param(("[Pasted: ", ""), id="empty_prefix"),
        pytest.param(["[Pasted: "], id="list"),
        pytest.param((b"[Pasted: ",), id="non_string_prefix"),
    ],
)
def test_declaring_malformed_paste_placeholders_fails_when_the_class_is_defined(declared_prefixes: object) -> None:
    with pytest.raises(InvalidPastePlaceholderPrefixesError):
        type(
            "_MalformedPlaceholderTuiAgent",
            (_ProbeTuiAgent,),
            {"PASTE_PLACEHOLDER_PREFIXES": declared_prefixes, "__module__": __name__},
        )


def test_a_concrete_tui_agent_that_leaves_out_paste_placeholders_fails_when_the_class_is_defined() -> None:
    with pytest.raises(InvalidPastePlaceholderPrefixesError, match="must declare PASTE_PLACEHOLDER_PREFIXES"):

        class _UndeclaredPlaceholderTuiAgent(InteractiveTuiAgent[AgentTypeConfig]):
            TUI_READY_INDICATOR = "probe-banner"

            def _build_enter_retry_gate(self, message: str) -> EnterRetryGate | None:
                return None

            def _build_submission_evidence_probes(
                self, message: str, policy: SubmissionConfirmationPolicy
            ) -> Sequence[SubmissionEvidenceProbe]:
                return []


def test_an_abstract_tui_agent_base_may_leave_the_paste_placeholders_to_its_subclasses() -> None:
    class _AbstractTuiAgentBase(InteractiveTuiAgent[AgentTypeConfig]):
        TUI_READY_INDICATOR = "probe-banner"

    class _ConcreteTuiAgent(_AbstractTuiAgentBase):
        PASTE_PLACEHOLDER_PREFIXES = ()

        def _build_enter_retry_gate(self, message: str) -> EnterRetryGate | None:
            return None

        def _build_submission_evidence_probes(
            self, message: str, policy: SubmissionConfirmationPolicy
        ) -> Sequence[SubmissionEvidenceProbe]:
            return []

    assert inspect.isabstract(_AbstractTuiAgentBase)
    assert not hasattr(_AbstractTuiAgentBase, "PASTE_PLACEHOLDER_PREFIXES")
    assert not inspect.isabstract(_ConcreteTuiAgent)


def test_send_message_confirms_through_the_agents_own_enter_retry_gate() -> None:
    """The subclass's own gate, not ``build_unsubmitted_message_retry_gate``'s, is what the confirmation script runs before a retry."""

    class _FooterGatedTuiAgent(_RecordingTuiAgent):
        def _build_enter_retry_gate(self, message: str) -> EnterRetryGate | None:
            return EnterRetryGate(condition_command=NonEmptyStr("test -e /tmp/footer-gate-marker-28417"))

    agent = _make_recording_agent(
        f"probe-banner {_RESUME_MESSAGE}",
        CommandResult(stdout="MNGR_CONFIRMED never\n", stderr="", success=True),
        probes=[_never_confirming_probe()],
        agent_class=_FooterGatedTuiAgent,
    )
    agent.send_message(_RESUME_MESSAGE)
    confirmation_commands = [c for c in cast(ScriptedHost, agent.host).sent_commands if "MNGR_CONFIRMED" in c]
    assert len(confirmation_commands) == 1
    retry_loop = confirmation_commands[0][confirmation_commands[0].index("while :") :]
    assert "test -e /tmp/footer-gate-marker-28417" in retry_loop
    assert shlex.quote(build_normalized_message_probe(_RESUME_MESSAGE)) not in retry_loop
    assert _PANE_SHOWS_MESSAGE_FUNCTION not in retry_loop


@pytest.mark.allow_warnings
def test_wait_for_tui_ready_raises_when_indicator_never_appears() -> None:
    """When the indicator never appears, the readiness wait raises instead of returning.

    ``send_message`` calls ``wait_for_tui_ready`` before pasting, so if the pane
    never shows the indicator (e.g. a transcript still replaying), this raise is
    what stops keystrokes from being dropped into a not-yet-ready session. A
    short timeout keeps the test off the production 30s default.
    """
    agent = _make_recording_agent("restored conversation, no prompt yet")
    with pytest.raises(SendMessageError, match="Timeout waiting for TUI to be ready"):
        wait_for_tui_ready(agent, agent.tmux_target, agent.get_tui_ready_indicator(), timeout_seconds=0.2)


@pytest.mark.allow_warnings
def test_wait_for_tui_ready_string_indicator_matches_literally() -> None:
    """A plain ``str`` indicator is an exact substring -- regex metacharacters are literal.

    The matching mode is chosen by the indicator's type, never by its contents, so
    the string "a.b" matches the literal "a.b" but not "axb".
    """
    present = _make_recording_agent("ready: a.b prompt")
    wait_for_tui_ready(present, present.tmux_target, "a.b", timeout_seconds=0.2)

    absent = _make_recording_agent("ready: axb prompt")
    with pytest.raises(SendMessageError, match="Timeout waiting for TUI to be ready"):
        wait_for_tui_ready(absent, absent.tmux_target, "a.b", timeout_seconds=0.2)


def test_wait_for_tui_ready_pattern_indicator_matches_as_regex() -> None:
    """A compiled ``re.Pattern`` indicator is matched with ``re.search``."""
    agent = _make_recording_agent("ready: axb prompt")
    wait_for_tui_ready(agent, agent.tmux_target, re.compile(r"a.b"), timeout_seconds=0.2)


def test_send_message_runs_preflight_before_readiness_wait() -> None:
    """A blocking preflight condition must surface immediately, not hang on readiness.

    A blocking dialog occupies the pane, so the ready indicator never appears
    while it is up. If the readiness wait ran before preflight, send_message
    would block for the full readiness timeout instead of raising. Preflight
    must run first: here the pane lacks the indicator, yet the preflight error
    is raised promptly (not a "Timeout waiting for TUI" readiness error).
    """

    class _PreflightRaisingAgent(_RecordingTuiAgent):
        def _preflight_send_message(self, tmux_target: TmuxWindowTarget | str) -> str | None:
            self.steps.append("preflight")
            raise SendMessageError(str(self.name), "blocking dialog")

    agent = _PreflightRaisingAgent.model_construct(
        name=AgentName("probe"),
        pane_content="",
        steps=[],
        built_policies=[],
        probes_to_return=[],
        host=ScriptedHost(),
    )
    with pytest.raises(SendMessageError, match="blocking dialog"):
        agent.send_message("hello")
    assert agent.steps == ["preflight"]
