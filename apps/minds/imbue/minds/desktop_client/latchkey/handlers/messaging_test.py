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


def test_provider_lookup_narrows_try_send_to_one_provider() -> None:
    caller = RecordingMngrCaller()
    sender = MngrMessageSender(
        caller=caller,
        provider_lookup=lambda target: "imbue_cloud_x" if target == "agent-1" else None,
    )

    assert sender.try_send("agent-1", "hello") is True
    # ``--provider`` skips scanning every enabled provider during discovery.
    assert caller.calls == [["message", "--provider", "imbue_cloud_x", "-m", "hello", "--", "agent-1"]]


def test_provider_lookup_returning_none_falls_back_to_full_scan() -> None:
    caller = RecordingMngrCaller()
    sender = MngrMessageSender(caller=caller, provider_lookup=lambda target: None)

    assert sender.try_send("unknown-target", "hello") is True
    assert caller.calls == [["message", "-m", "hello", "--", "unknown-target"]]


def test_provider_lookup_narrows_deliver_to_one_provider() -> None:
    delivered_stdout = '{"event": "message_sent", "agent": "assistant", "message": "ok"}\n'
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=delivered_stdout))
    sender = MngrMessageSender(caller=caller, provider_lookup=lambda target: "prov-1")

    assert sender.deliver("assistant", "hello") is True
    assert caller.calls == [["message", "--format", "jsonl", "--provider", "prov-1", "-m", "hello", "--", "assistant"]]
