import base64
import json
import shlex
import threading
from collections.abc import Sequence

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.welcome_chat import SEED_SCRIPT_PATH
from imbue.minds.desktop_client.welcome_chat import SEED_TIMEOUT_SECONDS
from imbue.minds.desktop_client.welcome_chat import WelcomeChatOutcome
from imbue.minds.desktop_client.welcome_chat import WelcomeChatRequest
from imbue.minds.desktop_client.welcome_chat import WelcomeChatRole
from imbue.minds.desktop_client.welcome_chat import WelcomeChatSeeder
from imbue.minds.desktop_client.welcome_chat import WelcomeChatTurn
from imbue.minds.desktop_client.welcome_chat import build_seed_welcome_chat_args
from imbue.minds.desktop_client.welcome_chat import seed_welcome_chat
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import HookedMngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
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


_ATTEMPT_ID = CreateAttemptId("create-attempt-0123456789abcdef0123456789abcdef")
_SEEDED = MngrCallResult(returncode=0, stdout='{"chat_id": "agent-seeded"}\n', is_mngr_output=True)
_REFUSED = MngrCallResult(
    returncode=1, stderr="Could not seed the welcome chat: the chat app answered HTTP 503\n", is_mngr_output=True
)


def _seeder(caller: MngrCaller) -> WelcomeChatSeeder:
    return WelcomeChatSeeder(mngr_caller=caller, backend_resolver=MngrCliBackendResolver())


def _seeded_transcript(argv: Sequence[str]) -> dict[str, object]:
    return json.loads(base64.b64decode(shlex.split(argv[3])[3]).decode("utf-8"))


def test_a_conversation_handed_over_while_the_workspace_was_built_is_seeded_when_the_attempt_finishes() -> None:
    """The creation page may be gone by then: the user left it and entered the workspace from the list."""
    caller = RecordingMngrCaller(result=_SEEDED)
    seeder = _seeder(caller)

    seeder.hold(_ATTEMPT_ID, _request())
    seeder.seed_held(_ATTEMPT_ID, _AGENT_ID)

    (argv,) = caller.calls
    assert argv[:3] == ["exec", "--agent", str(_AGENT_ID)]
    assert _seeded_transcript(argv) == _request().model_dump(mode="json")


def test_an_attempt_whose_page_handed_nothing_over_is_not_seeded() -> None:
    caller = RecordingMngrCaller(result=_SEEDED)

    _seeder(caller).seed_held(_ATTEMPT_ID, _AGENT_ID)

    assert caller.calls == []


def test_a_page_asking_while_the_finished_attempts_seed_runs_waits_for_it_instead_of_seeding_a_second_chat() -> None:
    release = threading.Event()
    caller = HookedMngrCaller(result=_SEEDED)

    def hold_the_seed_until_released(_argv: Sequence[str]) -> None:
        release.wait(timeout=30.0)

    caller.set_on_call(hold_the_seed_until_released)
    seeder = _seeder(caller)
    seeder.hold(_ATTEMPT_ID, _request())
    background = threading.Thread(target=seeder.seed_held, args=(_ATTEMPT_ID, _AGENT_ID))
    background.start()
    assert caller.called_event.wait(timeout=30.0)

    outcomes: list[WelcomeChatOutcome] = []
    later_body = WelcomeChatRequest(title="Later", turns=(WelcomeChatTurn(role=WelcomeChatRole.USER, text="hi"),))
    page = threading.Thread(target=lambda: outcomes.append(seeder.seed(_ATTEMPT_ID, _AGENT_ID, later_body)))
    page.start()
    page.join(timeout=0.2)
    assert page.is_alive()

    release.set()
    page.join(timeout=30.0)
    background.join(timeout=30.0)

    assert outcomes == [WelcomeChatOutcome(chat_id="agent-seeded")]
    (argv,) = caller.calls
    assert _seeded_transcript(argv)["title"] == "Welcome"


def test_a_failed_seed_is_run_again_on_the_next_ask_and_a_seeded_one_is_never_repeated() -> None:
    caller = ScriptedMngrCaller(results=(_REFUSED, _SEEDED))
    seeder = _seeder(caller)
    seeder.hold(_ATTEMPT_ID, _request())

    seeder.seed_held(_ATTEMPT_ID, _AGENT_ID)
    retried = seeder.seed(_ATTEMPT_ID, _AGENT_ID, _request())
    again = seeder.seed(_ATTEMPT_ID, _AGENT_ID, _request())

    assert retried == WelcomeChatOutcome(chat_id="agent-seeded")
    assert again == retried
    assert len(caller.calls) == 2


def test_a_seed_that_could_not_run_mngr_answers_a_failure_rather_than_leaving_its_waiters_hanging() -> None:
    caller = HookedMngrCaller(result=_SEEDED)

    def refuse_to_run(_argv: Sequence[str]) -> None:
        raise OSError("the warm process is gone")

    caller.set_on_call(refuse_to_run)

    outcome = _seeder(caller).seed(_ATTEMPT_ID, _AGENT_ID, _request())

    assert not outcome.is_seeded
    assert outcome.failure_detail == "mngr could not be run: the warm process is gone"
