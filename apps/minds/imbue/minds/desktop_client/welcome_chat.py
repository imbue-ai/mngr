"""Continue the onboarding conversation inside a new workspace as its first chat.

The start flow and the creation page are a conversation: the "what is honest software"
exchange, the questions about where to run the workspace, the settings the user chose, the
setup and ready lines. Once the workspace is up that conversation carries on inside it: the
creation page hands the turns to this module, which runs the template's
``system/scripts/seed_welcome_chat.py`` in the workspace through ``mngr exec``. The script
posts the turns to the chat app's seed route and prints the chat's id; the chat app lists the
chat with the transcript on its page, and the user's first message there launches the
workspace's first real agent.

The transcript rides the command line as base64 (``mngr exec`` carries no stdin, and the
turns hold newlines and quotes). The script itself retries the chat app for a bounded window
while the workspace's services are still coming up, so the call here is given a little longer
than that window.
"""

import base64
import json
import shlex
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.in_workspace_mngr import in_workspace_failure_detail
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller

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
