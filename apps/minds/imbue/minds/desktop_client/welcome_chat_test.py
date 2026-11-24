import io
import json
import re
import shlex
import threading
from collections.abc import Sequence
from pathlib import Path

import httpx2
from starlette.testclient import TestClient

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.testing import FAKE_FORWARD_PORT
from imbue.minds.desktop_client.testing import FIXED_WELCOME_CHAT_SEED_BODY
from imbue.minds.desktop_client.testing import ForwardHandler
from imbue.minds.desktop_client.testing import answering_forward
from imbue.minds.desktop_client.testing import make_fake_chat_app_forward_route
from imbue.minds.desktop_client.testing import welcome_chat_seed_body_sent_through_mngr_exec
from imbue.minds.desktop_client.welcome_chat import ChatAppForwardRoute
from imbue.minds.desktop_client.welcome_chat import ForwardSeedAttempt
from imbue.minds.desktop_client.welcome_chat import SEED_SCRIPT_PATH
from imbue.minds.desktop_client.welcome_chat import SEED_TIMEOUT_SECONDS
from imbue.minds.desktop_client.welcome_chat import WelcomeChatOutcome
from imbue.minds.desktop_client.welcome_chat import WelcomeChatSeeder
from imbue.minds.desktop_client.welcome_chat import build_seed_welcome_chat_args
from imbue.minds.desktop_client.welcome_chat import seed_welcome_chat
from imbue.minds.desktop_client.welcome_chat import seed_welcome_chat_through_forward
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import HookedMngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_forward.auth import FileAuthStore
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.server import create_forward_app
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager

_AGENT_ID = AgentId("agent-0123456789abcdef0123456789abcdef")


def test_the_seed_runs_the_templates_script_in_the_workspace_with_the_fixed_body_as_base64() -> None:
    """``mngr exec`` carries no stdin, so the body rides argv encoded; the command must not start a workspace."""
    argv = build_seed_welcome_chat_args(_AGENT_ID)

    assert argv[:3] == ["exec", "--agent", str(_AGENT_ID)]
    assert argv[-1] == "--no-start"
    inner = shlex.split(argv[3])
    assert inner[:3] == ["python3", SEED_SCRIPT_PATH, "--transcript-base64"]
    assert welcome_chat_seed_body_sent_through_mngr_exec(argv) == FIXED_WELCOME_CHAT_SEED_BODY


def test_a_seeded_chat_reads_its_id_off_the_scripts_json_line() -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout='some chatter\n{"chat_id": "agent-seeded"}\n', is_mngr_output=True)
    )

    outcome = seed_welcome_chat(caller, _AGENT_ID)

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

    outcome = seed_welcome_chat(caller, _AGENT_ID)

    assert not outcome.is_seeded
    assert outcome.failure_detail == "Could not seed the welcome chat: the chat app answered HTTP 400"


def test_a_timed_out_or_silent_seed_is_a_failure_in_plain_words() -> None:
    timed_out = RecordingMngrCaller(result=MngrCallResult(returncode=1, is_timed_out=True))
    assert seed_welcome_chat(timed_out, _AGENT_ID).failure_detail == "the workspace did not answer in time"

    silent = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="nothing useful\n", is_mngr_output=True))
    assert seed_welcome_chat(silent, _AGENT_ID).failure_detail == "the seeding script printed no chat id"

    no_words = RecordingMngrCaller(result=MngrCallResult(returncode=3, is_mngr_output=True))
    assert seed_welcome_chat(no_words, _AGENT_ID).failure_detail == "the seeding script exited with code 3"

    # The caller's own account of a call that returned nothing is not a verdict of the workspace's.
    no_result = RecordingMngrCaller(
        result=MngrCallResult(returncode=1, stderr="mngr warm process exited without returning a result")
    )
    assert seed_welcome_chat(no_result, _AGENT_ID).failure_detail == "mngr returned no result"


_ATTEMPT_ID = CreateAttemptId("create-attempt-0123456789abcdef0123456789abcdef")
_SEEDED = MngrCallResult(returncode=0, stdout='{"chat_id": "agent-seeded"}\n', is_mngr_output=True)
_REFUSED = MngrCallResult(
    returncode=1, stderr="Could not seed the welcome chat: the chat app answered HTTP 503\n", is_mngr_output=True
)


def _seeder(caller: MngrCaller) -> WelcomeChatSeeder:
    return WelcomeChatSeeder(mngr_caller=caller, backend_resolver=MngrCliBackendResolver())


_SEED_STARTED_LINE = f"Seeding the welcome chat for create attempt {_ATTEMPT_ID} in workspace {_AGENT_ID}"


def _assert_seed_span_timed(lines: Sequence[str]) -> str:
    """Assert the seed's span closed once with its duration, and answer that closing line."""
    (closed,) = [line for line in lines if line.startswith(f"{_SEED_STARTED_LINE} [")]
    assert re.fullmatch(rf"{re.escape(_SEED_STARTED_LINE)} \[done in \d+\.\d{{5}} sec\]", closed)
    return closed


def _seed_warning_line(failure_detail: str) -> str:
    return (
        f"Could not seed the welcome chat for create attempt {_ATTEMPT_ID} in workspace {_AGENT_ID}: {failure_detail}"
    )


def test_a_seed_asked_for_while_the_workspace_was_built_runs_when_the_attempt_finishes() -> None:
    """The creation page may be gone by then: the user left it and entered the workspace from the list."""
    caller = RecordingMngrCaller(result=_SEEDED)
    seeder = _seeder(caller)

    seeder.owe(_ATTEMPT_ID)
    seeder.seed_if_owed(_ATTEMPT_ID, _AGENT_ID)

    (argv,) = caller.calls
    assert argv[:3] == ["exec", "--agent", str(_AGENT_ID)]
    assert welcome_chat_seed_body_sent_through_mngr_exec(argv) == FIXED_WELCOME_CHAT_SEED_BODY


def test_a_workspace_is_seeded_once_however_often_its_page_asks() -> None:
    """The page asks as it loads, again at done, and a revisited page asks again; the workspace is seeded once."""
    caller = RecordingMngrCaller(result=_SEEDED)
    seeder = _seeder(caller)

    seeder.owe(_ATTEMPT_ID)
    seeder.owe(_ATTEMPT_ID)
    seeder.seed_if_owed(_ATTEMPT_ID, _AGENT_ID)
    outcome = seeder.seed(_ATTEMPT_ID, _AGENT_ID)

    assert outcome == WelcomeChatOutcome(chat_id="agent-seeded")
    assert len(caller.calls) == 1


def test_an_attempt_whose_page_never_asked_is_not_seeded() -> None:
    """A create made through the API alone opens no creation page, and its workspace gets no welcome chat."""
    caller = RecordingMngrCaller(result=_SEEDED)
    seeder = _seeder(caller)
    seeder.owe(CreateAttemptId.generate())

    seeder.seed_if_owed(_ATTEMPT_ID, _AGENT_ID)

    assert caller.calls == []


def test_a_page_asking_while_the_finished_attempts_seed_runs_waits_for_it_instead_of_seeding_a_second_chat() -> None:
    release = threading.Event()
    caller = HookedMngrCaller(result=_SEEDED)

    def hold_the_seed_until_released(_argv: Sequence[str]) -> None:
        release.wait(timeout=30.0)

    caller.set_on_call(hold_the_seed_until_released)
    seeder = _seeder(caller)
    seeder.owe(_ATTEMPT_ID)
    background = threading.Thread(target=seeder.seed_if_owed, args=(_ATTEMPT_ID, _AGENT_ID))
    background.start()
    assert caller.called_event.wait(timeout=30.0)

    outcomes: list[WelcomeChatOutcome] = []
    page = threading.Thread(target=lambda: outcomes.append(seeder.seed(_ATTEMPT_ID, _AGENT_ID)))
    page.start()
    page.join(timeout=0.2)
    assert page.is_alive()

    release.set()
    page.join(timeout=30.0)
    background.join(timeout=30.0)

    assert outcomes == [WelcomeChatOutcome(chat_id="agent-seeded")]
    assert len(caller.calls) == 1


def test_a_failed_seed_is_run_again_on_the_next_ask_and_a_seeded_one_is_never_repeated() -> None:
    caller = ScriptedMngrCaller(results=(_REFUSED, _SEEDED))
    seeder = _seeder(caller)
    seeder.owe(_ATTEMPT_ID)

    seeder.seed_if_owed(_ATTEMPT_ID, _AGENT_ID)
    retried = seeder.seed(_ATTEMPT_ID, _AGENT_ID)
    again = seeder.seed(_ATTEMPT_ID, _AGENT_ID)

    assert retried == WelcomeChatOutcome(chat_id="agent-seeded")
    assert again == retried
    assert len(caller.calls) == 2


def test_a_seed_that_could_not_run_mngr_answers_a_failure_rather_than_leaving_its_waiters_hanging() -> None:
    caller = HookedMngrCaller(result=_SEEDED)

    def refuse_to_run(_argv: Sequence[str]) -> None:
        raise OSError("the warm process is gone")

    caller.set_on_call(refuse_to_run)

    with capture_loguru(level="WARNING") as log_output:
        outcome = _seeder(caller).seed(_ATTEMPT_ID, _AGENT_ID)

    assert not outcome.is_seeded
    assert outcome.failure_detail == "mngr could not be run: the warm process is gone"
    assert log_output.getvalue().splitlines() == [
        _seed_warning_line("mngr could not be run: the warm process is gone")
    ]


def test_a_seed_logs_its_start_its_duration_and_the_chat_it_opened() -> None:
    """The hand-off into a new workspace is measured from these lines in minds.log."""
    with capture_loguru(level="DEBUG") as log_output:
        _seeder(RecordingMngrCaller(result=_SEEDED)).seed(_ATTEMPT_ID, _AGENT_ID)

    lines = log_output.getvalue().splitlines()
    closed = _assert_seed_span_timed(lines)
    seeded = f"Seeded welcome chat agent-seeded for create attempt {_ATTEMPT_ID}"
    assert lines.index(_SEED_STARTED_LINE) < lines.index(closed) < lines.index(seeded)


def test_a_failed_seed_logs_its_duration_and_one_warning_with_the_reason() -> None:
    with capture_loguru(level="DEBUG") as log_output:
        _seeder(RecordingMngrCaller(result=_REFUSED)).seed(_ATTEMPT_ID, _AGENT_ID)

    lines = log_output.getvalue().splitlines()
    _assert_seed_span_timed(lines)
    assert [line for line in lines if line.startswith("Could not seed the welcome chat for create attempt")] == [
        _seed_warning_line("Could not seed the welcome chat: the chat app answered HTTP 503")
    ]
    assert not [line for line in lines if line.startswith("Seeded welcome chat")]


_SEED_URL = f"https://127.0.0.1:{FAKE_FORWARD_PORT}/api/chats/seed"
_CHAT_ORIGIN_HOST = f"chat.{_AGENT_ID}.localhost"


_CREATED = httpx2.Response(201, json={"chat_id": "agent-over-forward", "name": "main", "display_name": "Main"})
_FORWARD_NOT_ROUTED = httpx2.Response(503, text="Backend not yet available")
_CHAT_APP_NOT_READY = httpx2.Response(
    503, json={"detail": "The chat app has not read its agent list from mngr yet; try again shortly."}
)


def _forward_seeder(caller: MngrCaller, route: ChatAppForwardRoute) -> WelcomeChatSeeder:
    return WelcomeChatSeeder(mngr_caller=caller, backend_resolver=MngrCliBackendResolver(), chat_app_route=route)


def _seed_over(route: ChatAppForwardRoute) -> ForwardSeedAttempt:
    with route.make_client() as client:
        return seed_welcome_chat_through_forward(route, client, _AGENT_ID)


def test_the_seed_is_posted_to_the_chat_apps_origin_through_the_forward_without_running_mngr() -> None:
    requests, handler = answering_forward(_CREATED)
    caller = RecordingMngrCaller(result=_SEEDED)

    outcome = _forward_seeder(caller, make_fake_chat_app_forward_route(handler)).seed(_ATTEMPT_ID, _AGENT_ID)

    assert outcome == WelcomeChatOutcome(chat_id="agent-over-forward")
    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == _SEED_URL
    assert request.headers["host"] == _CHAT_ORIGIN_HOST
    assert json.loads(request.content) == FIXED_WELCOME_CHAT_SEED_BODY
    assert caller.calls == []


def test_the_forward_route_retries_while_the_chat_app_cannot_be_reached_or_is_not_ready() -> None:
    requests, handler = answering_forward(
        httpx2.ConnectError("connection refused"), _FORWARD_NOT_ROUTED, _CHAT_APP_NOT_READY, _CREATED
    )

    attempt = _seed_over(make_fake_chat_app_forward_route(handler))

    assert attempt == ForwardSeedAttempt(outcome=WelcomeChatOutcome(chat_id="agent-over-forward"))
    assert len(requests) == 4


def test_a_seed_the_forward_cannot_deliver_within_its_window_goes_through_mngr_exec() -> None:
    _requests, handler = answering_forward(_CHAT_APP_NOT_READY)
    caller = RecordingMngrCaller(result=_SEEDED)

    with capture_loguru(level="INFO") as log_output:
        outcome = _forward_seeder(caller, make_fake_chat_app_forward_route(handler, window_seconds=0.0)).seed(
            _ATTEMPT_ID, _AGENT_ID
        )

    assert outcome == WelcomeChatOutcome(chat_id="agent-seeded")
    (argv,) = caller.calls
    assert argv[:3] == ["exec", "--agent", str(_AGENT_ID)]
    assert (
        f"Could not seed the welcome chat in workspace {_AGENT_ID} through the forward (not delivered within 0s: "
        "The chat app has not read its agent list from mngr yet; try again shortly.); seeding it through mngr exec"
    ) in log_output.getvalue().splitlines()


def test_an_answer_only_an_unreachable_route_gives_sends_the_seed_through_mngr_exec() -> None:
    """A redirect, an auth refusal, or a chat app from before the seed route: the route, not the seed, is at fault."""
    for answer in (
        httpx2.Response(302, headers={"Location": "https://localhost:8421/goto/"}),
        httpx2.Response(403, text="Not authenticated"),
        httpx2.Response(404, json={"detail": "Not Found"}),
    ):
        requests, handler = answering_forward(answer)
        caller = RecordingMngrCaller(result=_SEEDED)

        outcome = _forward_seeder(caller, make_fake_chat_app_forward_route(handler)).seed(_ATTEMPT_ID, _AGENT_ID)

        assert outcome == WelcomeChatOutcome(chat_id="agent-seeded")
        assert len(requests) == 1
        assert len(caller.calls) == 1


def test_a_route_whose_client_cannot_be_built_sends_the_seed_through_mngr_exec() -> None:
    def no_ca_certificate() -> httpx2.Client:
        raise FileNotFoundError("no such file: forward-ca.pem")

    caller = RecordingMngrCaller(result=_SEEDED)
    route = ChatAppForwardRoute(forward_port=FAKE_FORWARD_PORT, make_client=no_ca_certificate)

    outcome = _forward_seeder(caller, route).seed(_ATTEMPT_ID, _AGENT_ID)

    assert outcome == WelcomeChatOutcome(chat_id="agent-seeded")
    assert len(caller.calls) == 1


def test_a_request_that_failed_after_it_went_out_is_not_retried_over_the_forward() -> None:
    requests, handler = answering_forward(httpx2.ReadTimeout("timed out"))

    attempt = _seed_over(make_fake_chat_app_forward_route(handler))

    assert attempt == ForwardSeedAttempt(unavailable_reason="the request failed: ReadTimeout: timed out")
    assert len(requests) == 1


def test_the_chat_apps_refusal_is_the_seeds_outcome_rather_than_a_reason_to_run_mngr() -> None:
    _requests, handler = answering_forward(httpx2.Response(409, json={"detail": "A chat named Main already exists"}))
    caller = RecordingMngrCaller(result=_SEEDED)

    outcome = _forward_seeder(caller, make_fake_chat_app_forward_route(handler)).seed(_ATTEMPT_ID, _AGENT_ID)

    assert outcome == WelcomeChatOutcome(
        failure_detail="the chat app refused the seed: A chat named Main already exists"
    )
    assert caller.calls == []


def test_a_created_answer_without_a_chat_id_is_a_failed_seed() -> None:
    _requests, handler = answering_forward(httpx2.Response(201, json={"name": "main"}))

    attempt = _seed_over(make_fake_chat_app_forward_route(handler))

    assert attempt == ForwardSeedAttempt(
        outcome=WelcomeChatOutcome(failure_detail="the chat app answered without a chat id")
    )


def test_a_seed_through_the_forward_logs_its_start_its_duration_and_the_chat_it_opened() -> None:
    _requests, handler = answering_forward(_CREATED)

    with capture_loguru(level="DEBUG") as log_output:
        _forward_seeder(RecordingMngrCaller(result=_SEEDED), make_fake_chat_app_forward_route(handler)).seed(
            _ATTEMPT_ID, _AGENT_ID
        )

    lines = log_output.getvalue().splitlines()
    closed = _assert_seed_span_timed(lines)
    seeded = f"Seeded welcome chat agent-over-forward for create attempt {_ATTEMPT_ID}"
    assert lines.index(_SEED_STARTED_LINE) < lines.index(closed) < lines.index(seeded)


_PREAUTH_COOKIE = "preauth-welcome-seed"
_WORKSPACE_HOST_ID = HostId("host-0123456789abcdef0123456789abcdef")


def _seed_through_a_real_forward(
    tmp_path: Path, chat_app: ForwardHandler, cookie: str
) -> tuple[ForwardSeedAttempt, list[httpx2.Request]]:
    """Seed through the ``mngr forward`` app itself, whose chat service is ``chat_app``, with ``cookie`` as the session."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = AgentInstanceKey.build(_AGENT_ID, _WORKSPACE_HOST_ID)
    resolver.add_known_agent(instance_key)
    # As a workspace registers them: each service under a minted ``<name>-<rand>`` origin label.
    resolver.update_services(
        instance_key,
        {"system_interface": "http://shell.invalid", "chat": "http://chat.invalid"},
        {"system-interface-a1b2c3": "system_interface", "chat-d4e5f6": "chat"},
    )
    forward = create_forward_app(
        auth_store=FileAuthStore(data_directory=tmp_path),
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=FAKE_FORWARD_PORT,
        preauth_cookie_value=_PREAUTH_COOKIE,
        allow_host_loopback=True,
    )
    backend_requests: list[httpx2.Request] = []

    def backend(request: httpx2.Request) -> httpx2.Response:
        backend_requests.append(request)
        return chat_app(request)

    with TestClient(
        forward, base_url=f"https://127.0.0.1:{FAKE_FORWARD_PORT}", cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie}
    ) as client:
        forward.state.http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(backend))
        route = ChatAppForwardRoute(
            forward_port=FAKE_FORWARD_PORT, make_client=lambda: client, window_seconds=5.0, retry_interval_seconds=0.01
        )
        attempt = seed_welcome_chat_through_forward(route, client, _AGENT_ID)
    return attempt, backend_requests


def test_the_forward_routes_the_chat_apps_name_to_its_service_and_retries_until_it_is_ready(tmp_path: Path) -> None:
    """The chat app's origin is addressed by its service name, not its minted label, with the session cookie."""
    _chat_requests, chat_app = answering_forward(_CHAT_APP_NOT_READY, _CREATED)

    attempt, backend_requests = _seed_through_a_real_forward(tmp_path, chat_app, _PREAUTH_COOKIE)

    assert attempt == ForwardSeedAttempt(outcome=WelcomeChatOutcome(chat_id="agent-over-forward"))
    assert [str(request.url) for request in backend_requests] == ["http://chat.invalid/api/chats/seed"] * 2
    assert json.loads(backend_requests[-1].content) == FIXED_WELCOME_CHAT_SEED_BODY
    # The forward keeps its own session cookie from the workspace's services.
    assert MNGR_FORWARD_SESSION_COOKIE_NAME not in backend_requests[-1].headers.get("cookie", "")


def test_a_forward_that_refuses_the_session_leaves_the_seed_to_mngr_exec(tmp_path: Path) -> None:
    _chat_requests, chat_app = answering_forward(_CREATED)

    attempt, backend_requests = _seed_through_a_real_forward(tmp_path, chat_app, "not-the-preauth-cookie")

    assert attempt == ForwardSeedAttempt(unavailable_reason="the forward answered HTTP 403")
    assert backend_requests == []
