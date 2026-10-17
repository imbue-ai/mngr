import base64
import json
import shlex

from imbue.minds.desktop_client.welcome_chat import SEED_SCRIPT_PATH
from imbue.minds.desktop_client.welcome_chat import SEED_TIMEOUT_SECONDS
from imbue.minds.desktop_client.welcome_chat import WelcomeChatRequest
from imbue.minds.desktop_client.welcome_chat import WelcomeChatRole
from imbue.minds.desktop_client.welcome_chat import WelcomeChatTurn
from imbue.minds.desktop_client.welcome_chat import build_seed_welcome_chat_args
from imbue.minds.desktop_client.welcome_chat import seed_welcome_chat
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId

_AGENT_ID = AgentId("agent-0123456789abcdef0123456789abcdef")


def _request() -> WelcomeChatRequest:
    return WelcomeChatRequest(
        title="Welcome",
        turns=(
            WelcomeChatTurn(role=WelcomeChatRole.USER, text="Wait.. what is honest software?"),
            WelcomeChatTurn(role=WelcomeChatRole.ASSISTANT, text='It works "for you".\nNo lock-in.'),
        ),
    )


def test_the_seed_runs_the_templates_script_in_the_workspace_with_the_transcript_as_base64() -> None:
    """The transcript holds quotes and newlines, so it rides argv encoded; the command must not start a workspace."""
    argv = build_seed_welcome_chat_args(_AGENT_ID, _request())

    assert argv[:3] == ["exec", "--agent", str(_AGENT_ID)]
    assert argv[-1] == "--no-start"
    inner = shlex.split(argv[3])
    assert inner[:3] == ["python3", SEED_SCRIPT_PATH, "--transcript-base64"]
    decoded = json.loads(base64.b64decode(inner[3]).decode("utf-8"))
    assert decoded == {
        "title": "Welcome",
        "turns": [
            {"role": "user", "text": "Wait.. what is honest software?"},
            {"role": "assistant", "text": 'It works "for you".\nNo lock-in.'},
        ],
    }


def test_a_seeded_chat_reads_its_id_off_the_scripts_json_line() -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout='some chatter\n{"chat_id": "agent-seeded"}\n', is_mngr_output=True)
    )

    outcome = seed_welcome_chat(caller, _AGENT_ID, _request())

    assert outcome.is_seeded and outcome.chat_id == "agent-seeded"
    (recorded,) = caller.recorded_calls
    assert recorded.timeout == SEED_TIMEOUT_SECONDS


def test_a_failed_seed_carries_the_scripts_verdict_rather_than_the_outer_execs() -> None:
    """``mngr exec`` closes a failed run with its own ``Command failed`` line; the script's words come before it."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=1,
            stderr=(
                "WARNING: something skipped\n"
                "Could not seed the welcome chat: the chat app answered HTTP 400\n"
                f"ERROR: Command failed on agent {_AGENT_ID}\n"
            ),
            is_mngr_output=True,
        )
    )

    outcome = seed_welcome_chat(caller, _AGENT_ID, _request())

    assert not outcome.is_seeded
    assert outcome.failure_detail == "Could not seed the welcome chat: the chat app answered HTTP 400"


def test_a_timed_out_or_silent_seed_is_a_failure_in_plain_words() -> None:
    timed_out = RecordingMngrCaller(result=MngrCallResult(returncode=1, is_timed_out=True))
    assert seed_welcome_chat(timed_out, _AGENT_ID, _request()).failure_detail == "the workspace did not answer in time"

    silent = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="nothing useful\n", is_mngr_output=True))
    assert seed_welcome_chat(silent, _AGENT_ID, _request()).failure_detail == "the seeding script printed no chat id"

    no_words = RecordingMngrCaller(result=MngrCallResult(returncode=3, is_mngr_output=True))
    assert seed_welcome_chat(no_words, _AGENT_ID, _request()).failure_detail == "the seeding script exited with code 3"

    # The caller's own account of a call that returned nothing is not a verdict of the workspace's.
    no_result = RecordingMngrCaller(
        result=MngrCallResult(returncode=1, stderr="mngr warm process exited without returning a result")
    )
    assert seed_welcome_chat(no_result, _AGENT_ID, _request()).failure_detail == "mngr returned no result"
