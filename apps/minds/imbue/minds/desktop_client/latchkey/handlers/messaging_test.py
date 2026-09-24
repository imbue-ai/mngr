import shlex

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.chat_app import MESSAGE_CHAT_SCRIPT
from imbue.minds.desktop_client.chat_app import build_message_chat_args
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import format_resolution_notice
from imbue.minds.desktop_client.latchkey.handlers.messaging import message_failure_reason
from imbue.minds.desktop_client.latchkey.handlers.messaging import mngr_message_argv
from imbue.minds.desktop_client.latchkey.handlers.messaging import stdout_reports_message_delivered
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.testing import exec_result_stdout
from imbue.minds.desktop_client.testing import script_exit_stdout
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
from imbue.mngr.cli.exec import exec_command
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


# What ``mngr message --format jsonl`` prints on delivery, as the inner stdout of its exec.
_MESSAGE_SENT_STDOUT = exec_result_stdout('{"event": "message_sent", "agent": "assistant", "message": "ok"}\n')
# The exec of the messaging script, by its verdict.
_SCRIPT_DELIVERED = MngrCallResult(returncode=0, stdout=script_exit_stdout(0))
_SCRIPT_REFUSED = MngrCallResult(
    returncode=0,
    stdout=script_exit_stdout(1, inner_stderr="The chat app refused the message: the chat is converging\n"),
)
_SCRIPT_MISSING = MngrCallResult(
    returncode=0,
    stdout=script_exit_stdout(
        2, inner_stderr="python3: can't open file '/home/user/workspace/system/scripts/message_chat.py'\n"
    ),
)
# The exec never reached the workspace: no result event at all.
_WORKSPACE_DOWN = MngrCallResult(returncode=1, stderr="Agent is not running (state: STOPPED)", is_mngr_output=True)


def test_message_failure_reason_reports_the_inner_error() -> None:
    stdout = (
        "WARNING: provider chatter\n"
        '{"event": "message_error", "agent": "assistant", "error": "Agent is not running (state: STOPPED)"}\n'
    )
    assert message_failure_reason(stdout) == "Agent is not running (state: STOPPED)"
    assert message_failure_reason("WARNING: provider chatter\n") == ""


def test_send_does_not_raise_on_failure(root_concurrency_group: ConcurrencyGroup) -> None:
    caller = RecordingMngrCaller(result=_WORKSPACE_DOWN)
    # An empty retry schedule keeps the eventually-failing send to one attempt.
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group, retry_delays_seconds=())

    # Fire-and-forget: dispatching an eventually-failing send must not raise.
    chat_id = AgentId()
    sender.send(chat_id, "hello", exec_agent_id=chat_id)
    # Let the background delivery run so the failure path is exercised.
    assert caller.called_event.wait(5.0)


def test_send_dispatches_on_concurrency_group_thread(root_concurrency_group: ConcurrencyGroup) -> None:
    caller = RecordingMngrCaller(result=_SCRIPT_DELIVERED)
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)
    agent_id = AgentId()

    # Fire-and-forget: send returns without waiting for the delivery to run.
    sender.send(agent_id, "hello", exec_agent_id=agent_id)

    assert caller.called_event.wait(5.0)
    # send goes to the chat's own chat app first, by chat id.
    assert caller.calls == [build_message_chat_args(str(agent_id), [str(agent_id), "-m", "hello"])]


def test_send_retries_until_the_agent_receives_the_message(root_concurrency_group: ConcurrencyGroup) -> None:
    """A resolution that races the agent's lifecycle still lands once the agent is back.

    The chat's verdict badge and the agent's resume both ride on this one
    message, so an undelivered attempt (agent stopped / mid-restart) must be
    retried rather than dropped.
    """
    caller = ScriptedMngrCaller(results=(_WORKSPACE_DOWN, _SCRIPT_REFUSED, _SCRIPT_DELIVERED))
    sender = MngrMessageSender(
        mngr_caller=caller,
        concurrency_group=root_concurrency_group,
        retry_delays_seconds=(0.01, 0.01, 0.01),
    )

    assert sender._send_with_retries("some-agent", "hello", "some-agent") is True
    assert len(caller.calls) == 3


def test_send_retries_abandon_on_shutdown(root_concurrency_group: ConcurrencyGroup) -> None:
    caller = RecordingMngrCaller(result=_WORKSPACE_DOWN)
    sender = MngrMessageSender(
        mngr_caller=caller,
        concurrency_group=root_concurrency_group,
        retry_delays_seconds=(30.0,),
    )
    root_concurrency_group.shutdown_event.set()

    # With the shutdown event already set, the backoff wait returns
    # immediately and the retry loop abandons instead of sleeping 30s.
    assert sender._send_with_retries("some-agent", "hello", "some-agent") is False
    assert len(caller.calls) == 1


def test_deliver_goes_through_the_chats_own_chat_app_first(root_concurrency_group: ConcurrencyGroup) -> None:
    """The request's agent id is the chat's id, and the chat may have moved to another agent since,
    so the nudge is handed to the workspace's chat app, which knows which agent takes it now."""
    caller = RecordingMngrCaller(result=_SCRIPT_DELIVERED)
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("agent-chat", "hello", "agent-chat") is True
    assert caller.calls == [build_message_chat_args("agent-chat", ["agent-chat", "-m", "hello"])]


def test_deliver_runs_the_script_on_the_chats_agent_and_names_the_chat(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A seeded chat's id is its seed's, not an agent's: the script runs on the agent the resolver
    named for the chat and still addresses the chat by its own id."""
    caller = RecordingMngrCaller(result=_SCRIPT_DELIVERED)
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("agent-seeded-chat", "hello", "agent-member") is True
    assert len(caller.calls) == 1
    context = exec_command.make_context("mngr exec", caller.calls[0][1:])
    assert [address.agent for address in context.params["agent_list"]] == ["agent-member"]
    script, _, _ = context.params["command_arg"].rpartition("; ")
    assert shlex.split(script)[2:] == ["python3", MESSAGE_CHAT_SCRIPT, "agent-seeded-chat", "-m", "hello"]


def test_deliver_true_when_the_notice_landed_behind_a_dialog(root_concurrency_group: ConcurrencyGroup) -> None:
    """Exit 7 is delivered-but-blocked: the text is already in the pane, and a retry would type
    it in again on every tick of the ramp."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0, stdout=script_exit_stdout(7, inner_stderr="Delivered, but its input is blocked\n")
        )
    )
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("agent-chat", "hello", "agent-chat") is True
    assert len(caller.calls) == 1


def test_deliver_falls_back_to_an_in_workspace_mngr_message_only_when_the_script_gave_no_verdict(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A template from before the script: python exits 2 with no chat-app verdict, so the app's
    own ``mngr message`` runs -- inside the workspace, where every harness's send path works."""
    caller = ScriptedMngrCaller(results=(_SCRIPT_MISSING, MngrCallResult(returncode=0, stdout=_MESSAGE_SENT_STDOUT)))
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello", "assistant") is True
    assert caller.calls == [
        build_message_chat_args("assistant", ["assistant", "-m", "hello"]),
        mngr_message_argv("assistant", "hello"),
    ]


def test_mngr_message_argv_runs_the_send_inside_the_agents_own_workspace() -> None:
    argv = mngr_message_argv("assistant", "hello (resolution: granted)")

    context = exec_command.make_context("mngr exec", argv[1:])
    assert [address.agent for address in context.params["agent_list"]] == ["assistant"]
    assert context.params["start"] is False
    inner = shlex.split(context.params["command_arg"])
    assert inner[2:] == [
        "mngr",
        "message",
        "--format",
        "jsonl",
        "-m",
        "hello (resolution: granted)",
        "--",
        "assistant",
    ]


def test_deliver_false_when_the_chat_app_refuses_and_never_sends_around_it(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # A refusal (exit 1 with the chat app's own words) is the chat app holding or refusing the
    # message on purpose; a direct send would land on the agent the chat is leaving.
    caller = RecordingMngrCaller(result=_SCRIPT_REFUSED)
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello", "assistant") is False
    assert len(caller.calls) == 1


def test_deliver_false_without_a_fallback_when_the_exec_never_reached_the_workspace(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # No exec result at all says nothing about the script; the caller's ramp tries again later.
    caller = RecordingMngrCaller(result=_WORKSPACE_DOWN)
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello", "assistant") is False
    assert len(caller.calls) == 1


def test_deliver_false_when_mngr_message_exits_zero_but_no_message_sent_event(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # The key regression on the backoff path: exit 0 with no message_sent event (agent not
    # found yet) must NOT be treated as delivered.
    caller = ScriptedMngrCaller(results=(_SCRIPT_MISSING, MngrCallResult(returncode=0, stdout=exec_result_stdout(""))))
    sender = MngrMessageSender(mngr_caller=caller, concurrency_group=root_concurrency_group)

    assert sender.deliver("assistant", "hello", "assistant") is False


def test_send_keeps_retrying_at_the_final_interval_after_the_ramp(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The backoff ramp does not end in a give-up: its last interval repeats.

    A verdict for a stopped workspace must land whenever that workspace next
    comes up during this app run, not only within the first two minutes.
    """
    caller = ScriptedMngrCaller(results=(_WORKSPACE_DOWN, _WORKSPACE_DOWN, _WORKSPACE_DOWN, _SCRIPT_DELIVERED))
    # A two-entry ramp; the fourth attempt only happens if the final interval repeats.
    sender = MngrMessageSender(
        mngr_caller=caller,
        concurrency_group=root_concurrency_group,
        retry_delays_seconds=(0.01, 0.01),
    )

    assert sender._send_with_retries("some-agent", "denied", "some-agent") is True
    assert len(caller.calls) == 4
