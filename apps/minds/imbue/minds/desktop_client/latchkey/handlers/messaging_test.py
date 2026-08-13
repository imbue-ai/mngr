from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import stdout_reports_message_delivered
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


def test_stdout_reports_delivered_true_for_message_sent_event() -> None:
    stdout = '{"event": "message_sent", "agent": "assistant", "message": "Message sent successfully"}\n'
    assert stdout_reports_message_delivered(stdout) is True


def test_stdout_reports_delivered_false_when_no_agent_matched() -> None:
    # "No agents found" produces no message_sent event even though mngr exits 0.
    assert stdout_reports_message_delivered("") is False


def test_stdout_reports_delivered_ignores_non_json_and_error_events() -> None:
    stdout = 'WARNING: some noise line\n{"event": "message_error", "agent": "assistant", "error": "boom"}\n'
    assert stdout_reports_message_delivered(stdout) is False


_DELIVERED_STDOUT = '{"event": "message_sent", "agent": "assistant", "message": "ok"}\n'


class _ScriptedMngrCaller(RecordingMngrCaller):
    """RecordingMngrCaller returning one scripted result per call (last one repeats)."""

    results: tuple[MngrCallResult, ...] = Field(description="Results returned call-by-call; the last one repeats.")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        index = min(len(self.calls), len(self.results) - 1)
        super().call(argv, timeout=timeout, env_overrides=env_overrides, cwd=cwd)
        return self.results[index]


def test_send_does_not_raise_on_failure(root_concurrency_group: ConcurrencyGroup) -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="agent missing"))
    # An empty retry schedule keeps the eventually-failing send to one attempt.
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group, retry_delays_seconds=())

    # Fire-and-forget: dispatching an eventually-failing send must not raise.
    sender.send(AgentId(), "hello")
    # Let the background delivery run so the failure path is exercised.
    assert caller.called_event.wait(5.0)


def test_send_dispatches_on_concurrency_group_thread(root_concurrency_group: ConcurrencyGroup) -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=_DELIVERED_STDOUT))
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)
    agent_id = AgentId()

    # Fire-and-forget: send returns without waiting for the delivery to run.
    sender.send(agent_id, "hello")

    assert caller.called_event.wait(5.0)
    # send delivers via the jsonl form so the message_sent event is observable.
    assert caller.calls == [["message", "--format", "jsonl", "-m", "hello", "--", str(agent_id)]]


def test_send_retries_until_the_agent_receives_the_message(root_concurrency_group: ConcurrencyGroup) -> None:
    """A resolution that races the agent's lifecycle still lands once the agent is back.

    The chat's verdict badge and the agent's resume both ride on this one
    message, so an undelivered attempt (agent stopped / mid-restart) must be
    retried rather than dropped.
    """
    caller = _ScriptedMngrCaller(
        results=(
            MngrCallResult(returncode=1, stderr="Agent is not running (state: STOPPED)"),
            MngrCallResult(returncode=0, stdout=""),
            MngrCallResult(returncode=0, stdout=_DELIVERED_STDOUT),
        ),
    )
    sender = MngrMessageSender(
        mngr_caller=caller,
        concurrency_group=root_concurrency_group,
        retry_delays_seconds=(0.01, 0.01, 0.01),
    )

    assert sender._send_with_retries("some-agent", "hello") is True
    assert len(caller.calls) == 3


def test_send_retries_abandon_on_shutdown(root_concurrency_group: ConcurrencyGroup) -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=""))
    sender = MngrMessageSender(
        mngr_caller=caller,
        concurrency_group=root_concurrency_group,
        retry_delays_seconds=(30.0,),
    )
    root_concurrency_group.shutdown_event.set()

    # With the shutdown event already set, the backoff wait returns
    # immediately and the retry loop abandons instead of sleeping 30s.
    assert sender._send_with_retries("some-agent", "hello") is False
    assert len(caller.calls) == 1


def test_deliver_uses_jsonl_output_and_reports_delivered(root_concurrency_group: ConcurrencyGroup) -> None:
    delivered_stdout = '{"event": "message_sent", "agent": "assistant", "message": "ok"}\n'
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=delivered_stdout))
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello") is True
    assert caller.calls == [["message", "--format", "jsonl", "-m", "hello", "--", "assistant"]]


def test_deliver_false_when_exit_zero_but_no_message_sent_event(root_concurrency_group: ConcurrencyGroup) -> None:
    # The key regression: exit 0 with no message_sent event (agent not found
    # yet) must NOT be treated as delivered.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=""))
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello") is False
