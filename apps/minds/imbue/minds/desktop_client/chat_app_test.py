import shlex
import subprocess

import pytest

from imbue.minds.desktop_client.chat_app import ChatAppVerdict
from imbue.minds.desktop_client.chat_app import EXIT_SENTINEL
from imbue.minds.desktop_client.chat_app import MESSAGE_CHAT_SCRIPT
from imbue.minds.desktop_client.chat_app import ask_chat_app
from imbue.minds.desktop_client.chat_app import build_message_chat_args
from imbue.minds.desktop_client.chat_app import build_message_chat_command
from imbue.minds.desktop_client.chat_app import inner_exit_code
from imbue.minds.desktop_client.testing import exec_error_stdout
from imbue.minds.desktop_client.testing import script_exit_stdout
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.cli.exec import exec_command


def test_the_script_command_echoes_the_scripts_own_exit_status() -> None:
    """The shell the exec runs echoes what the script exited with, so the status survives the
    exec's boolean result; the argument list stays one shell-quoted invocation before it."""
    command = build_message_chat_command(["agent-1", "-m", "granted (resolution: granted, request_id: r)"])
    completed = subprocess.run(
        ["sh", "-c", command.replace(f"python3 {MESSAGE_CHAT_SCRIPT}", "sh -c 'exit 7' --")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert inner_exit_code(completed.stdout) == 7
    assert inner_exit_code("") is None
    assert inner_exit_code(f"{EXIT_SENTINEL}not-a-number\n") is None


def test_a_message_argument_cannot_break_out_of_the_script_command() -> None:
    hostile = "x'; echo pwned; true"
    command = build_message_chat_command(["agent-1", "-m", hostile])
    script, _, echo = command.rpartition("; ")
    assert echo == f"echo {EXIT_SENTINEL}$?"
    assert shlex.split(script)[-1] == hostile


def test_the_args_are_one_agent_and_one_shell_command_to_mngr_exec() -> None:
    """``mngr exec`` takes one shell string as the command after the agents, so the script
    invocation must be quoted into a single argument or the message text is run as the command
    and the other tokens are taken for agent names."""
    text = "Your request for Slack was granted. (resolution: granted, request_id: evt-abc123)"
    argv = build_message_chat_args("agent-member", ["agent-chat", "-m", text])

    context = exec_command.make_context("mngr exec", argv[1:])
    assert [address.agent for address in context.params["agent_list"]] == ["agent-member"]
    assert context.params["start"] is False
    script, _, _ = context.params["command_arg"].rpartition("; ")
    # The env assignments lead, so the script's own ``mngr`` backoff resolves the image's
    # copy and tolerates the workspace's unknown config (see in_workspace_mngr).
    assert shlex.split(script) == [
        "PATH=/root/.local/bin:$PATH",
        "MNGR_ALLOW_UNKNOWN_CONFIG=1",
        "python3",
        MESSAGE_CHAT_SCRIPT,
        "agent-chat",
        "-m",
        text,
    ]


@pytest.mark.parametrize(
    ("result", "verdict", "detail"),
    (
        (
            MngrCallResult(returncode=0, stdout=script_exit_stdout(0, '{"chat_id": "agent-1"}\n')),
            ChatAppVerdict.DELIVERED,
            "",
        ),
        (
            MngrCallResult(returncode=0, stdout=script_exit_stdout(7, inner_stderr="Delivered, but blocked\n")),
            ChatAppVerdict.DELIVERED_BEHIND_DIALOG,
            "Delivered, but blocked",
        ),
        (
            MngrCallResult(
                returncode=0,
                stdout=script_exit_stdout(1, inner_stderr="Retrying\nThe chat app refused: the chat is converging\n"),
            ),
            ChatAppVerdict.REFUSED,
            "The chat app refused: the chat is converging",
        ),
        (
            MngrCallResult(
                returncode=0,
                stdout=script_exit_stdout(
                    2, inner_stderr="message_chat.py: error: unrecognized arguments: --create\n"
                ),
            ),
            ChatAppVerdict.NO_VERDICT,
            "message_chat.py: error: unrecognized arguments: --create",
        ),
        (
            MngrCallResult(
                returncode=1,
                stdout=exec_error_stdout("Agent chat-1 is not running (state: STOPPED)"),
                stderr="WARNING: outer SSH unreachable for host host-other\n",
                is_mngr_output=True,
            ),
            ChatAppVerdict.UNANSWERED,
            "Agent chat-1 is not running (state: STOPPED)",
        ),
    ),
    ids=("delivered", "behind-dialog", "refused", "no-verdict", "unanswered"),
)
def test_the_answer_is_read_from_the_scripts_exit_status_and_its_own_last_words(
    result: MngrCallResult, verdict: ChatAppVerdict, detail: str
) -> None:
    caller = RecordingMngrCaller(result=result)

    answer = ask_chat_app(caller, "agent-member", ["agent-chat", "-m", "hi"], timeout=10.0)

    assert answer.verdict is verdict
    assert answer.detail == detail
    assert caller.calls == [build_message_chat_args("agent-member", ["agent-chat", "-m", "hi"])]
