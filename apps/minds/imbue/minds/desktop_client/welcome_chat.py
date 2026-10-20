"""Continue the onboarding conversation inside a new workspace as its first chat.

The start flow and the creation page are a conversation: the "what is honest software"
exchange, the questions about where to run the workspace, the settings the user chose, the
setup and ready lines. Once the workspace is up that conversation carries on inside it: the
creation page hands the turns to this module, which runs the template's
``system/scripts/seed_welcome_chat.py`` in the workspace through ``mngr exec``. The script
posts the turns to the chat app's seed route and prints the chat's id; the chat app lists the
chat with the transcript on its page, and the user's first message there launches the
workspace's first real agent.

The page hands the conversation over as soon as it has it, while the workspace is still being
built, and the seed runs when the create attempt finishes (``WelcomeChatSeeder``), so a
workspace the user enters some other way -- from the workspace list, after leaving the creation
page -- still opens on its welcome chat.

The transcript rides the command line as base64 (``mngr exec`` carries no stdin, and the
turns hold newlines and quotes). The script itself retries the chat app for a bounded window
while the workspace's services are still coming up, so the call here is given a little longer
than that window.
"""

import base64
import json
import shlex
import threading
from concurrent.futures import Future
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.in_workspace_mngr import in_workspace_failure_detail
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# The template's seeding script, relative to the repo root ``mngr exec`` runs from.
SEED_SCRIPT_PATH: Final[str] = "system/scripts/seed_welcome_chat.py"
# Longer than the script's own retry window (120 s), so a slow chat app is the script's
# verdict rather than a killed call.
SEED_TIMEOUT_SECONDS: Final[float] = 150.0


class WelcomeChatRole(LowerCaseStrEnum):
    """Who said a turn of the onboarding conversation."""

    USER = auto()
    ASSISTANT = auto()


class WelcomeChatTurn(FrozenModel):
    """One turn, as the chat app's seed route takes it."""

    role: WelcomeChatRole = Field(description="The side of the conversation the turn belongs to")
    text: str = Field(description="The turn's text, as markdown")


class WelcomeChatRequest(FrozenModel):
    """The body the creation page posts once the workspace is ready: the chat's name and its turns."""

    title: str = Field(default="", description="The chat's display name; empty lets the chat app number it")
    turns: tuple[WelcomeChatTurn, ...] = Field(min_length=1, description="The conversation so far, in order")


class WelcomeChatOutcome(FrozenModel):
    """What seeding produced: the chat's id, or the reason it did not happen."""

    chat_id: str = Field(default="", description="The seeded chat's id; empty when seeding failed")
    failure_detail: str = Field(default="", description="Why the chat was not seeded; empty on success")

    @property
    def is_seeded(self) -> bool:
        return self.chat_id != ""


def build_seed_welcome_chat_args(workspace_address: str, welcome_chat: WelcomeChatRequest) -> list[str]:
    """The ``mngr`` args that run the template's seeding script in the workspace with the transcript."""
    body = json.dumps(welcome_chat.model_dump(mode="json"))
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


def seed_welcome_chat(
    mngr_caller: MngrCaller, workspace_address: str, welcome_chat: WelcomeChatRequest
) -> WelcomeChatOutcome:
    """Seed the workspace's first chat with the conversation; the chat's id, or why there is none."""
    result = mngr_caller.call(
        build_seed_welcome_chat_args(workspace_address, welcome_chat), timeout=SEED_TIMEOUT_SECONDS
    )
    if result.returncode == 0 and not result.is_timed_out:
        chat_id = _chat_id_from_stdout(result.stdout)
        if chat_id:
            return WelcomeChatOutcome(chat_id=chat_id)
        detail = "the seeding script printed no chat id"
    else:
        detail = _failure_detail(result)
    logger.warning("Could not seed the welcome chat in workspace {}: {}", workspace_address, detail)
    return WelcomeChatOutcome(failure_detail=detail)


class WelcomeChatSeeder(MutableModel):
    """Seeds each create attempt's welcome chat in its new workspace once, whether or not the creation page is there.

    The page hands the conversation over while the workspace is still being built (``hold``),
    and the agent creator's done hook seeds it the moment the attempt finishes (``seed_held``).
    A page still open at that moment asks for the seed itself (``seed``) and waits on the one
    already running instead of starting a second chat. A seed that failed is not kept, so the
    next ask runs it again.
    """

    model_config = {"arbitrary_types_allowed": True}

    mngr_caller: MngrCaller = Field(frozen=True, description="How the seeding script is run in the workspace")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Resolves the workspace's address")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _held_by_attempt: dict[CreateAttemptId, WelcomeChatRequest] = PrivateAttr(default_factory=dict)
    _seed_by_attempt: dict[CreateAttemptId, Future[WelcomeChatOutcome]] = PrivateAttr(default_factory=dict)

    def hold(self, create_attempt_id: CreateAttemptId, welcome_chat: WelcomeChatRequest) -> None:
        """Keep the attempt's conversation for its seed; the first one handed over is the one seeded."""
        with self._lock:
            self._held_by_attempt.setdefault(create_attempt_id, welcome_chat)

    def is_held(self, create_attempt_id: CreateAttemptId) -> bool:
        with self._lock:
            return create_attempt_id in self._held_by_attempt

    def seed(
        self, create_attempt_id: CreateAttemptId, workspace_agent_id: AgentId, welcome_chat: WelcomeChatRequest
    ) -> WelcomeChatOutcome:
        """Seed the finished attempt's workspace now, or wait for the seed already under way, and answer its outcome."""
        self.hold(create_attempt_id, welcome_chat)
        return self._seed_once(create_attempt_id, workspace_agent_id)

    def seed_held(self, create_attempt_id: CreateAttemptId, workspace_agent_id: AgentId) -> None:
        """The agent creator's done hook: seed the conversation the page handed over, if it handed one over."""
        if self.is_held(create_attempt_id):
            self._seed_once(create_attempt_id, workspace_agent_id)

    def _seed_once(self, create_attempt_id: CreateAttemptId, workspace_agent_id: AgentId) -> WelcomeChatOutcome:
        own: Future[WelcomeChatOutcome] = Future()
        with self._lock:
            running = self._seed_by_attempt.get(create_attempt_id)
            if running is None:
                self._seed_by_attempt[create_attempt_id] = own
            welcome_chat = self._held_by_attempt[create_attempt_id]
        if running is not None:
            return running.result()
        address = build_agent_address(workspace_agent_id, self.backend_resolver)
        try:
            outcome = seed_welcome_chat(self.mngr_caller, address, welcome_chat)
        except OSError as e:
            # Whoever waits on this seed must get an answer, so a call that never ran is a failed seed.
            logger.warning("Could not run the welcome chat's seed in workspace {}: {}", address, e)
            outcome = WelcomeChatOutcome(failure_detail=f"mngr could not be run: {e}")
        if not outcome.is_seeded:
            with self._lock:
                del self._seed_by_attempt[create_attempt_id]
        own.set_result(outcome)
        return outcome
