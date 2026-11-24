"""Seed a new workspace's welcome chat, the chat its first run plays in.

Every creation page asks for the same seed (``WELCOME_CHAT_SEED_BODY``): a chat named "Welcome"
with no turns, flagged as a first-run arrival. This module posts it to the chat app's seed
route (``POST /api/chats/seed``) and gets the chat's id back; the workspace then owes its owner
the provider page and opens that chat on its first conversation.

The page asks as soon as it loads, while the workspace is still being built, and the seed runs
when the create attempt finishes (``WelcomeChatSeeder``), so a workspace the user enters some
other way -- from the workspace list, after leaving the creation page -- still plays its
arrival. A create whose creation page never asked (one made through the API alone) is not
seeded.

The seed goes to the chat app's own origin through ``mngr forward`` (``ChatAppForwardRoute``),
retried every quarter second while the forward cannot route it yet or the chat app is not ready
to take it. When that route is not configured, or cannot deliver the seed (it keeps failing past
its window, or answers the way only a route that cannot reach the chat app does), the seed falls
back to running the template's ``system/scripts/seed_welcome_chat.py`` in the workspace through
``mngr exec``, which posts the same body from inside. The body rides that command line as
base64 (``mngr exec`` carries no stdin). The script retries the chat app for a bounded window of
its own, so that call is given a little longer than the window.
"""

import base64
import json
import shlex
import threading
from collections.abc import Callable
from collections.abc import Mapping
from concurrent.futures import Future
from pathlib import Path
from typing import Final

import httpx2
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from tenacity import Retrying
from tenacity import retry_if_exception_type
from tenacity import stop_after_delay
from tenacity import wait_fixed

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import info_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.agent_creator import MNGR_FORWARD_SCHEME
from imbue.minds.desktop_client.agent_creator import make_workspace_probe_client
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.in_workspace_mngr import in_workspace_failure_detail
from imbue.minds.errors import MindError
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# The template's seeding script, relative to the repo root ``mngr exec`` runs from.
SEED_SCRIPT_PATH: Final[str] = "system/scripts/seed_welcome_chat.py"
# Longer than the script's own retry window (120 s), so a slow chat app is the script's
# verdict rather than a killed call.
SEED_TIMEOUT_SECONDS: Final[float] = 150.0

# The chat app's registered service name. Its origin through ``mngr forward`` is
# ``chat.<workspace id>.localhost``: the forward resolves an origin label it holds no mapping for
# as the service name itself, and the app's minted label is ``chat-<rand>``, never the bare name.
CHAT_SERVICE_NAME: Final[str] = "chat"
CHAT_SEED_PATH: Final[str] = "/api/chats/seed"
# How long the forward route retries a seed it cannot deliver yet before the seed falls back to
# ``mngr exec``: the chat app registers its service and reads its agent list a few seconds after
# the shell the readiness probe waits for answers.
FORWARD_SEED_WINDOW_SECONDS: Final[float] = 30.0
FORWARD_SEED_RETRY_INTERVAL_SECONDS: Final[float] = 0.25
FORWARD_SEED_REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0


# What the chat app's seed route is sent for every workspace: an empty chat that the workspace
# plays its first-run arrival in.
WELCOME_CHAT_SEED_BODY: Final[Mapping[str, object]] = {"title": "Welcome", "turns": (), "is_first_run": True}


class WelcomeChatOutcome(FrozenModel):
    """What seeding produced: the chat's id, or the reason it did not happen."""

    chat_id: str = Field(default="", description="The seeded chat's id; empty when seeding failed")
    failure_detail: str = Field(default="", description="Why the chat was not seeded; empty on success")

    @property
    def is_seeded(self) -> bool:
        return self.chat_id != ""


def build_seed_welcome_chat_args(workspace_address: str) -> list[str]:
    """The ``mngr`` args that run the template's seeding script in the workspace with the seed body."""
    body = json.dumps(WELCOME_CHAT_SEED_BODY)
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    command = shlex.join(["python3", SEED_SCRIPT_PATH, "--transcript-base64", encoded])
    # --no-start: the workspace was just created and is running; seeding must never boot one.
    return ["exec", "--agent", workspace_address, command, "--no-start"]


def _chat_id_from_stdout(stdout: str) -> str:
    """The chat id from the script's one JSON line (the last such line, past any chatter)."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("chat_id"), str):
            return parsed["chat_id"]
    return ""


def _failure_detail(result: MngrCallResult) -> str:
    """Why the seed failed: the script's own words when it gave any, else what is known about the call."""
    if result.is_timed_out:
        return "the workspace did not answer in time"
    if not result.is_mngr_output:
        # The stderr is MngrCaller's account of a call that returned nothing, not a verdict of the workspace's.
        return "mngr returned no result"
    detail = in_workspace_failure_detail(result.stderr)
    return detail if detail else f"the seeding script exited with code {result.returncode}"


def seed_welcome_chat(mngr_caller: MngrCaller, workspace_address: str) -> WelcomeChatOutcome:
    """Seed the workspace's welcome chat through ``mngr exec``; the chat's id, or why there is none."""
    result = mngr_caller.call(build_seed_welcome_chat_args(workspace_address), timeout=SEED_TIMEOUT_SECONDS)
    if result.returncode == 0 and not result.is_timed_out:
        chat_id = _chat_id_from_stdout(result.stdout)
        if chat_id:
            return WelcomeChatOutcome(chat_id=chat_id)
        return WelcomeChatOutcome(failure_detail="the seeding script printed no chat id")
    return WelcomeChatOutcome(failure_detail=_failure_detail(result))


class ChatAppForwardRoute(FrozenModel):
    """How the desktop reaches a workspace's chat app through ``mngr forward``: the proxy's port and a client for it."""

    forward_port: int = Field(description="The port the ``mngr forward`` proxy listens on, on loopback")
    make_client: Callable[[], httpx2.Client] = Field(
        description="Builds a client the proxy accepts: its session cookie set and its local CA trusted"
    )
    window_seconds: float = Field(
        default=FORWARD_SEED_WINDOW_SECONDS,
        description="How long a seed the route cannot deliver yet is retried before it falls back to mngr exec",
    )
    retry_interval_seconds: float = Field(
        default=FORWARD_SEED_RETRY_INTERVAL_SECONDS, description="The wait between those retries"
    )

    def chat_app_url(self, path: str) -> str:
        """The proxy's URL for a chat app path; which workspace's chat app rides the ``Host`` header."""
        return f"{MNGR_FORWARD_SCHEME}://127.0.0.1:{self.forward_port}{path}"


@pure
def chat_app_forward_host(workspace_agent_id: AgentId) -> str:
    """The ``Host`` the proxy routes to the workspace's chat app."""
    return f"{CHAT_SERVICE_NAME}.{workspace_agent_id}.localhost"


def make_chat_app_forward_route(forward_port: int, preauth_cookie: str, ca_cert_path: Path) -> ChatAppForwardRoute:
    """The route through the running proxy, with the client configuration the workspace readiness probe uses."""
    return ChatAppForwardRoute(
        forward_port=forward_port,
        make_client=lambda: make_workspace_probe_client(
            preauth_cookie=preauth_cookie,
            probe_timeout_seconds=FORWARD_SEED_REQUEST_TIMEOUT_SECONDS,
            ca_cert_path=ca_cert_path,
        ),
    )


class ForwardSeedAttempt(FrozenModel):
    """What the forward route made of a seed: an outcome to report, or why the seed should go through mngr exec."""

    outcome: WelcomeChatOutcome | None = Field(
        default=None, description="The chat app's verdict; None when the route could not deliver the seed"
    )
    unavailable_reason: str = Field(default="", description="Why the route could not deliver it; empty otherwise")


class _SeedNotDeliverableYetError(MindError):
    """The forward could not route the seed yet, or the chat app was not ready for it; nothing acted on it."""


def forward_answer_detail(response: httpx2.Response) -> str:
    """The ``detail`` of a JSON error answer (the chat app's own words), else the bare status."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), str) and body["detail"]:
        return body["detail"]
    return f"HTTP {response.status_code}"


def describe_forward_transport_failure(error: httpx2.TransportError) -> str:
    # httpx2 leaves several transport errors without a message, so the class name is all there is then.
    message = str(error)
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _post_seed(client: httpx2.Client, url: str, host: str) -> httpx2.Response:
    """Post the seed once; raises ``_SeedNotDeliverableYetError`` for the failures that are safe to retry."""
    try:
        response = client.post(url, headers={"Host": host}, json=WELCOME_CHAT_SEED_BODY)
    except (httpx2.ConnectError, httpx2.ConnectTimeout) as e:
        raise _SeedNotDeliverableYetError(describe_forward_transport_failure(e)) from e
    # Both the forward (no route to the chat app yet, or the app not listening) and the chat app (its
    # agent list not read yet) answer 503 for a request nothing acted on.
    if response.status_code == 503:
        raise _SeedNotDeliverableYetError(forward_answer_detail(response))
    return response


def seed_welcome_chat_through_forward(
    route: ChatAppForwardRoute, client: httpx2.Client, workspace_agent_id: AgentId
) -> ForwardSeedAttempt:
    """Post the seed to the workspace's chat app through the forward, retrying while it cannot be delivered yet.

    ``client`` is one the route built. The chat app's verdicts on the seed (its chat id, a refusal
    of the body, a name already taken) are outcomes. Everything else -- the window running out, a
    failure after the request went out, an answer only a route that cannot reach the chat app
    gives (a redirect, an auth refusal, a missing route on an older template) -- leaves the seed
    to ``mngr exec``. Sending it again that way cannot open a second chat: the chat app refuses a
    seed whose name is taken.
    """
    url = route.chat_app_url(CHAT_SEED_PATH)
    host = chat_app_forward_host(workspace_agent_id)
    try:
        for attempt in Retrying(
            retry=retry_if_exception_type(_SeedNotDeliverableYetError),
            stop=stop_after_delay(route.window_seconds),
            wait=wait_fixed(route.retry_interval_seconds),
            reraise=True,
        ):
            with attempt:
                response = _post_seed(client, url, host)
    except _SeedNotDeliverableYetError as e:
        return ForwardSeedAttempt(unavailable_reason=f"not delivered within {route.window_seconds:g}s: {e}")
    except httpx2.TransportError as e:
        return ForwardSeedAttempt(unavailable_reason=f"the request failed: {describe_forward_transport_failure(e)}")
    status = response.status_code
    if 200 <= status < 300:
        try:
            answer = response.json()
        except ValueError:
            answer = None
        chat_id = answer.get("chat_id") if isinstance(answer, dict) else None
        if isinstance(chat_id, str) and chat_id:
            return ForwardSeedAttempt(outcome=WelcomeChatOutcome(chat_id=chat_id))
        return ForwardSeedAttempt(outcome=WelcomeChatOutcome(failure_detail="the chat app answered without a chat id"))
    if status in (400, 409):
        return ForwardSeedAttempt(
            outcome=WelcomeChatOutcome(
                failure_detail=f"the chat app refused the seed: {forward_answer_detail(response)}"
            )
        )
    return ForwardSeedAttempt(unavailable_reason=f"the forward answered {forward_answer_detail(response)}")


def _seed_through_route(route: ChatAppForwardRoute, workspace_agent_id: AgentId) -> ForwardSeedAttempt:
    try:
        client = route.make_client()
    except OSError as e:
        # The proxy's CA certificate could not be read.
        return ForwardSeedAttempt(unavailable_reason=f"its client could not be built: {e}")
    with client:
        return seed_welcome_chat_through_forward(route, client, workspace_agent_id)


class WelcomeChatSeeder(MutableModel):
    """Seeds each create attempt's welcome chat in its new workspace once, whether or not the creation page is there.

    The page asks while the workspace is still being built (``owe``), and the agent creator's
    done hook seeds the workspace the moment the attempt finishes (``seed_if_owed``). A page
    still open at that moment asks for the seed itself (``seed``) and waits on the one already
    running instead of starting a second chat. A seed that failed is not kept, so the next ask
    runs it again.
    """

    model_config = {"arbitrary_types_allowed": True}

    mngr_caller: MngrCaller = Field(frozen=True, description="How the seeding script is run in the workspace")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Resolves the workspace's address")
    chat_app_route: ChatAppForwardRoute | None = Field(
        default=None,
        frozen=True,
        description="Posts the seed to the chat app through mngr forward; None seeds through mngr exec alone",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _owed_attempts: set[CreateAttemptId] = PrivateAttr(default_factory=set)
    _seed_by_attempt: dict[CreateAttemptId, Future[WelcomeChatOutcome]] = PrivateAttr(default_factory=dict)

    def owe(self, create_attempt_id: CreateAttemptId) -> None:
        """Record that the attempt's creation page asked for the seed, to run when the attempt finishes."""
        with self._lock:
            self._owed_attempts.add(create_attempt_id)

    def is_owed(self, create_attempt_id: CreateAttemptId) -> bool:
        with self._lock:
            return create_attempt_id in self._owed_attempts

    def seed_if_owed(self, create_attempt_id: CreateAttemptId, workspace_agent_id: AgentId) -> None:
        """The agent creator's done hook: seed the workspace when its creation page asked for it."""
        if self.is_owed(create_attempt_id):
            self.seed(create_attempt_id, workspace_agent_id)

    def seed(self, create_attempt_id: CreateAttemptId, workspace_agent_id: AgentId) -> WelcomeChatOutcome:
        """Seed the finished attempt's workspace now, or wait for the seed already under way, and answer its outcome."""
        own: Future[WelcomeChatOutcome] = Future()
        with self._lock:
            running = self._seed_by_attempt.get(create_attempt_id)
            if running is None:
                self._seed_by_attempt[create_attempt_id] = own
        if running is not None:
            return running.result()
        address = build_agent_address(workspace_agent_id, self.backend_resolver)
        # info_span rather than log_span: its timed close is at DEBUG, so the seed's duration reaches minds.log.
        with info_span("Seeding the welcome chat for create attempt {} in workspace {}", create_attempt_id, address):
            outcome = self._deliver(workspace_agent_id, address)
        if outcome.is_seeded:
            logger.debug("Seeded welcome chat {} for create attempt {}", outcome.chat_id, create_attempt_id)
        else:
            logger.warning(
                "Could not seed the welcome chat for create attempt {} in workspace {}: {}",
                create_attempt_id,
                address,
                outcome.failure_detail,
            )
            with self._lock:
                del self._seed_by_attempt[create_attempt_id]
        own.set_result(outcome)
        return outcome

    def _deliver(self, workspace_agent_id: AgentId, address: str) -> WelcomeChatOutcome:
        """Seed through the forward when it can deliver the seed, else through mngr exec."""
        if self.chat_app_route is not None:
            attempt = _seed_through_route(self.chat_app_route, workspace_agent_id)
            if attempt.outcome is not None:
                return attempt.outcome
            logger.info(
                "Could not seed the welcome chat in workspace {} through the forward ({}); seeding it through mngr exec",
                address,
                attempt.unavailable_reason,
            )
        try:
            return seed_welcome_chat(self.mngr_caller, address)
        except OSError as e:
            # Whoever waits on this seed must get an answer, so a call that never ran is a failed seed.
            return WelcomeChatOutcome(failure_detail=f"mngr could not be run: {e}")
