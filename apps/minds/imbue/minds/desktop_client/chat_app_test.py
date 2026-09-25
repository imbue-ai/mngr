import shlex
import subprocess

import pytest

from imbue.minds.desktop_client.chat_app import ChatAppVerdict
from imbue.minds.desktop_client.chat_app import EXIT_SENTINEL
from imbue.minds.desktop_client.chat_app import MESSAGE_CHAT_SCRIPT
from imbue.minds.desktop_client.chat_app import ask_chat_app
from imbue.minds.desktop_client.chat_app import build_chat_app_exec_args
from imbue.minds.desktop_client.chat_app import build_message_chat_command
from imbue.minds.desktop_client.chat_app import inner_exit_code
from imbue.minds.desktop_client.chat_app import read_chat_app_answer
from imbue.minds.desktop_client.testing import exec_error_stdout
from imbue.minds.desktop_client.testing import exec_result_stdout
from imbue.minds.desktop_client.testing import script_exit_stdout
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.cli.exec import exec_command

_FALLBACK_COMMAND = "echo fallback-out; echo fallback-err >&2; exit 5"


def _run_with_script_exiting(exit_code: int, fallback_command: str) -> MngrCallResult:
    """Run the real command shell with the script replaced by one that exits ``exit_code``, answering as the exec would.

    ``mngr exec`` reports each stream as its lines joined, so neither keeps a trailing newline.
    """
    command = build_message_chat_command(["agent-1", "-m", "hi"], fallback_command)
    stubbed = command.replace(f"python3 {MESSAGE_CHAT_SCRIPT}", f"sh -c 'echo script-err >&2; exit {exit_code}' --")
    completed = subprocess.run(["sh", "-c", stubbed], capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    return MngrCallResult(
        returncode=0,
        stdout=exec_result_stdout(completed.stdout.removesuffix("\n"), completed.stderr.removesuffix("\n")),
    )


def test_the_script_command_echoes_the_scripts_own_exit_status_and_skips_the_fallback_on_a_verdict() -> None:
    """The shell echoes what the script exited with, so the status survives the exec's boolean result."""
    answer = read_chat_app_answer(_run_with_script_exiting(7, _FALLBACK_COMMAND))

    assert answer.script_exit_code == 7
    assert answer.verdict is ChatAppVerdict.DELIVERED_BEHIND_DIALOG
    assert answer.fallback is None
    assert inner_exit_code("") is None
    assert inner_exit_code(f"{EXIT_SENTINEL}not-a-number\n") is None


def test_a_script_with_no_verdict_runs_the_fallback_in_the_same_shell_and_reports_it_apart() -> None:
    """The fallback's output and status come back separately from the script's, so neither is read as the other's."""
    answer = read_chat_app_answer(_run_with_script_exiting(2, _FALLBACK_COMMAND))

    assert answer.verdict is ChatAppVerdict.NO_VERDICT
    assert answer.detail == "script-err"
    assert answer.fallback is not None
    assert answer.fallback.stdout == "fallback-out"
    assert answer.fallback.stderr.strip() == "fallback-err"
    assert answer.fallback.exit_code == 5


def test_a_fallback_silent_on_stderr_leaves_the_scripts_own_last_words_as_the_detail() -> None:
    """The sentinel that opens the fallback's stderr is then the stream's last line, not the script's reason."""
    answer = read_chat_app_answer(_run_with_script_exiting(2, "echo fallback-out"))

    assert answer.detail == "script-err"
    assert answer.fallback is not None
    assert answer.fallback.stdout == "fallback-out"
    assert answer.fallback.stderr == ""
    assert answer.fallback.exit_code == 0


def test_a_message_argument_cannot_break_out_of_the_script_command() -> None:
    hostile = "x'; echo pwned; true"
    command = build_message_chat_command(["agent-1", "-m", hostile], _FALLBACK_COMMAND)
    script, _, _ = command.partition("; mngr_chat_app_exit=")
    assert shlex.split(script)[-1] == hostile


def test_the_args_are_one_agent_and_one_shell_command_to_mngr_exec() -> None:
    """``mngr exec`` takes one shell string as the command after the agents, so the script
    invocation must be quoted into a single argument or the message text is run as the command
    and the other tokens are taken for agent names."""
    text = "Your request for Slack was granted. (resolution: granted, request_id: evt-abc123)"
    argv = build_chat_app_exec_args(
        "agent-member", build_message_chat_command(["agent-chat", "-m", text], _FALLBACK_COMMAND)
    )

    context = exec_command.make_context("mngr exec", argv[1:])
    assert [address.agent for address in context.params["agent_list"]] == ["agent-member"]
    assert context.params["start"] is False
    script, _, _ = context.params["command_arg"].partition("; mngr_chat_app_exit=")
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

    answer = ask_chat_app(
        caller, "agent-member", ["agent-chat", "-m", "hi"], fallback_command=_FALLBACK_COMMAND, timeout=10.0
    )

    assert answer.verdict is verdict
    assert answer.detail == detail
    assert caller.calls == [
        build_chat_app_exec_args(
            "agent-member", build_message_chat_command(["agent-chat", "-m", "hi"], _FALLBACK_COMMAND)
        )
    ]
