from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import format_resolution_notice
from imbue.minds.desktop_client.latchkey.handlers.messaging import is_message_chat_unavailable
from imbue.minds.desktop_client.latchkey.handlers.messaging import message_chat_argv
from imbue.minds.desktop_client.latchkey.handlers.messaging import stdout_reports_message_delivered
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


def test_format_resolution_notice_appends_the_verdict_and_request_id() -> None:
    notice = format_resolution_notice(
        "Your permission request for Slack was granted.", "evt-abc123", RequestStatus.GRANTED
    )
    assert notice == "Your permission request for Slack was granted. (resolution: granted, request_id: evt-abc123)"
    denied = format_resolution_notice("No.", "evt-d", RequestStatus.DENIED)
    assert denied == "No. (resolution: denied, request_id: evt-d)"
    # The original message stays a prefix, unmodified -- callers also return it
    # verbatim to the human user, where the appended tag would just be clutter.
    assert notice.startswith("Your permission request for Slack was granted.")


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
    # send goes to the chat's own chat app first, by chat id.
    assert caller.calls == [message_chat_argv(str(agent_id), "hello")]


def test_send_retries_until_the_agent_receives_the_message(root_concurrency_group: ConcurrencyGroup) -> None:
    """A resolution that races the agent's lifecycle still lands once the agent is back.

    The chat's verdict badge and the agent's resume both ride on this one
    message, so an undelivered attempt (agent stopped / mid-restart) must be
    retried rather than dropped.
    """
    caller = _ScriptedMngrCaller(
        results=(
            MngrCallResult(returncode=1, stderr="Agent is not running (state: STOPPED)"),
            MngrCallResult(returncode=1, stderr="the chat app has not read its agent list from mngr yet"),
            MngrCallResult(returncode=0, stdout=""),
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
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="Agent is not running (state: STOPPED)"))
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


_SCRIPT_MISSING_STDERR = (
    "python3: can't open file '/home/user/workspace/system/scripts/message_chat.py': "
    "[Errno 2] No such file or directory\n"
)


def test_deliver_goes_through_the_chats_own_chat_app_first(root_concurrency_group: ConcurrencyGroup) -> None:
    """The request's agent id is the chat's id, and the chat may have moved to another agent since,
    so the nudge is handed to the workspace's chat app, which knows which agent takes it now."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=""))
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("agent-chat", "hello") is True
    assert caller.calls == [message_chat_argv("agent-chat", "hello")]
    assert caller.calls[0][:3] == ["exec", "agent-chat", "--"]
    assert caller.calls[0][-3:] == ["agent-chat", "-m", "hello"]


def test_deliver_falls_back_to_mngr_message_only_when_the_workspace_has_no_script(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    delivered_stdout = '{"event": "message_sent", "agent": "assistant", "message": "ok"}\n'
    caller = _ScriptedMngrCaller(
        results=(
            MngrCallResult(returncode=1, stderr=_SCRIPT_MISSING_STDERR),
            MngrCallResult(returncode=0, stdout=delivered_stdout),
        )
    )
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello") is True
    assert caller.calls == [
        message_chat_argv("assistant", "hello"),
        ["message", "--format", "jsonl", "-m", "hello", "--", "assistant"],
    ]
    assert is_message_chat_unavailable(_SCRIPT_MISSING_STDERR) is True
    assert is_message_chat_unavailable("the chat app refused: the chat is moving to another agent") is False


def test_deliver_false_when_the_chat_app_refuses_and_never_sends_around_it(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # A refusal (exit 1 with the chat app's own words) is the chat app holding or refusing the
    # message on purpose; a direct send would land on the agent the chat is leaving.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="refused: the chat is converging"))
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello") is False
    assert len(caller.calls) == 1


def test_deliver_false_when_mngr_message_exits_zero_but_no_message_sent_event(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # The key regression on the backoff path: exit 0 with no message_sent event (agent not
    # found yet) must NOT be treated as delivered.
    caller = _ScriptedMngrCaller(
        results=(MngrCallResult(returncode=1, stderr=_SCRIPT_MISSING_STDERR), MngrCallResult(returncode=0, stdout=""))
    )
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello") is False


def test_send_keeps_retrying_at_the_final_interval_after_the_ramp(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The backoff ramp does not end in a give-up: its last interval repeats.

    A verdict for a stopped workspace must land whenever that workspace next
    comes up during this app run, not only within the first two minutes.
    """
    caller = _ScriptedMngrCaller(
        results=(
            MngrCallResult(returncode=1, stderr="Agent is not running (state: STOPPED)"),
            MngrCallResult(returncode=1, stderr="Agent is not running (state: STOPPED)"),
            MngrCallResult(returncode=1, stderr="Agent is not running (state: STOPPED)"),
            MngrCallResult(returncode=0, stdout=""),
        ),
    )
    # A two-entry ramp; the fourth attempt only happens if the final interval repeats.
    sender = MngrMessageSender(
        mngr_caller=caller,
        concurrency_group=root_concurrency_group,
        retry_delays_seconds=(0.01, 0.01),
    )

    assert sender._send_with_retries("some-agent", "denied") is True
    assert len(caller.calls) == 4
