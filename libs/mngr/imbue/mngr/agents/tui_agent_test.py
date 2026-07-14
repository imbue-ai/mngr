"""Unit tests for InteractiveTuiAgent's contract."""

import contextlib
import re
from typing import Final
from typing import Generator
from typing import Sequence
from typing import cast

import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.mock_host_test import ScriptedHost
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import SubmissionConfirmationPolicy
from imbue.mngr.agents.tui_utils import SubmissionEvidenceProbe
from imbue.mngr.agents.tui_utils import build_changed_token_probe
from imbue.mngr.agents.tui_utils import wait_for_tui_ready
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName


class _ProbeTuiAgent(InteractiveTuiAgent[AgentTypeConfig]):
    TUI_READY_INDICATOR = "probe-banner"

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
    steps: list[str] = []
    built_policies: list[SubmissionConfirmationPolicy] = []
    probes_to_return: list[SubmissionEvidenceProbe] = []

    @property
    def tmux_target(self) -> TmuxWindowTarget:
        return TmuxWindowTarget(session_name="s", window=0)

    @contextlib.contextmanager
    def _message_lock(self) -> Generator[None, None, None]:
        yield

    def _capture_pane_content(self, tmux_target: TmuxWindowTarget, include_scrollback: bool = False) -> str | None:
        return self.pane_content

    def _preflight_send_message(self, tmux_target: TmuxWindowTarget) -> None:
        self.steps.append("preflight")

    def _send_tmux_literal_keys(self, tmux_target: TmuxWindowTarget, message: str) -> None:
        self.steps.append("paste")

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
) -> _RecordingTuiAgent:
    host = ScriptedHost(scripted_results=list(scripted_results))
    return _RecordingTuiAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("probe"),
        pane_content=pane_content,
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


def test_send_message_waits_for_ready_indicator_before_pasting() -> None:
    """send_message must confirm the TUI ready indicator before any keystrokes.

    The readiness wait happens inside send_message (not only on the create
    path), so it covers resume too: pasting before the indicator is visible
    would drop keystrokes into a still-replaying transcript. With no evidence
    probes the send degrades to a single best-effort Enter on the host.
    """
    pane = f"probe-banner {_RESUME_MESSAGE}"
    agent = _make_recording_agent(pane)
    agent.send_message(_RESUME_MESSAGE)
    assert agent.steps == ["preflight", "paste"]
    host_commands = cast(ScriptedHost, agent.host).captured
    assert host_commands == ["tmux send-keys -t =s:0 Enter"]


def test_send_message_selects_strict_policy_for_normal_messages() -> None:
    agent = _make_recording_agent(f"probe-banner {_RESUME_MESSAGE}")
    agent.send_message(_RESUME_MESSAGE)
    assert agent.built_policies == [SubmissionConfirmationPolicy.STRICT]


def test_send_message_selects_relaxed_policy_for_slash_commands() -> None:
    agent = _make_recording_agent("probe-banner /clear")
    agent.send_message("/clear")
    assert agent.built_policies == [SubmissionConfirmationPolicy.RELAXED]


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

    host = ScriptedHost()
    agent = _LeftoverDetectingAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("probe"),
        pane_content=f"probe-banner {_RESUME_MESSAGE}",
        steps=[],
        built_policies=[],
        probes_to_return=[],
        host=host,
    )
    agent.send_message(_RESUME_MESSAGE)
    event_commands = [command for command in host.captured if "events/messages" in command]
    assert len(event_commands) == 1
    assert "preexisting_input_text" in event_commands[0]
    # The send still went through (best-effort Enter recorded after the event).
    assert any(command.endswith("Enter") for command in host.captured)


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
        def _preflight_send_message(self, tmux_target: TmuxWindowTarget) -> None:
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
