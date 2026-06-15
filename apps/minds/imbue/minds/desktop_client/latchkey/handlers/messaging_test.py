import threading
from collections.abc import Mapping
from collections.abc import Sequence

from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import stdout_reports_message_delivered
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


class _EventSignalingCaller(RecordingMngrCaller):
    """Recording caller that sets an event once a call has been recorded.

    Lets tests wait deterministically for a background send instead of polling.
    """

    _called_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
    ) -> MngrCallResult:
        result = super().call(argv, timeout=timeout, env_overrides=env_overrides)
        self._called_event.set()
        return result

    @property
    def called_event(self) -> threading.Event:
        return self._called_event


def test_stdout_reports_delivered_true_for_message_sent_event() -> None:
    stdout = '{"event": "message_sent", "agent": "assistant", "message": "Message sent successfully"}\n'
    assert stdout_reports_message_delivered(stdout) is True


def test_stdout_reports_delivered_false_when_no_agent_matched() -> None:
    # "No agents found" produces no message_sent event even though mngr exits 0.
    assert stdout_reports_message_delivered("") is False


def test_stdout_reports_delivered_ignores_non_json_and_error_events() -> None:
    stdout = 'WARNING: some noise line\n{"event": "message_error", "agent": "assistant", "error": "boom"}\n'
    assert stdout_reports_message_delivered(stdout) is False


def test_try_send_builds_message_argv() -> None:
    caller = RecordingMngrCaller()
    sender = MngrMessageSender(caller=caller)

    assert sender.try_send("some-agent", "hello") is True
    # The text must go through ``-m`` and the target after ``--`` so it is not
    # parsed as a second agent identifier.
    assert caller.calls == [["message", "-m", "hello", "--", "some-agent"]]


def test_try_send_returns_false_on_nonzero_exit() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="agent missing"))
    sender = MngrMessageSender(caller=caller)

    assert sender.try_send("missing-agent", "hello") is False


def test_send_does_not_raise_on_failure() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="agent missing"))
    sender = MngrMessageSender(caller=caller)

    # An undelivered nudge is recoverable, so ``send`` must never raise.
    sender.send(AgentId(), "hello")


def test_send_dispatches_on_concurrency_group_thread(root_concurrency_group: ConcurrencyGroup) -> None:
    caller = _EventSignalingCaller()
    sender = MngrMessageSender(caller=caller, concurrency_group=root_concurrency_group)
    agent_id = AgentId()

    # Fire-and-forget: send returns without waiting for the delivery to run.
    sender.send(agent_id, "hello")

    assert caller.called_event.wait(timeout=5.0)
    assert caller.calls == [["message", "-m", "hello", "--", str(agent_id)]]


def test_deliver_uses_jsonl_output_and_reports_delivered() -> None:
    delivered_stdout = '{"event": "message_sent", "agent": "assistant", "message": "ok"}\n'
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=delivered_stdout))
    sender = MngrMessageSender(caller=caller)

    assert sender.deliver("assistant", "hello") is True
    assert caller.calls == [["message", "--format", "jsonl", "-m", "hello", "--", "assistant"]]


def test_deliver_false_when_exit_zero_but_no_message_sent_event() -> None:
    # The key regression: exit 0 with no message_sent event (agent not found
    # yet) must NOT be treated as delivered.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=""))
    sender = MngrMessageSender(caller=caller)

    assert sender.deliver("assistant", "hello") is False
