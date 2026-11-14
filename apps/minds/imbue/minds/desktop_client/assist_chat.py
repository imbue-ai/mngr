"""The in-workspace ``/assist`` chat that helps diagnose a problem: what the app asks it to do, and how it starts."""

from collections.abc import Callable
from enum import auto
from typing import Final
from typing import assert_never

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.in_workspace_mngr import FAILURE_DETAIL_MAX_CHARS
from imbue.minds.desktop_client.skill_chat import SkillChatLaunch
from imbue.minds.desktop_client.skill_chat import SkillChatLaunchOutcome
from imbue.minds.desktop_client.workspace_lifecycle import MindHostActionOutcome

# Probed (via ``skill_chat``) before spawning: sending ``/assist <text>`` to a
# workspace whose template predates the skill makes Claude reject the unknown
# slash command, which never submits a prompt, so the ``mngr create --message``
# send hangs to its full timeout.
ASSIST_SKILL_NAME: Final[str] = "assist"


def build_assist_chat_message(description: str) -> str:
    """The seed prompt for an assist chat about ``description``."""
    return f"/assist {description}"


class AssistChatOutcome(UpperCaseStrEnum):
    """What asking a workspace for an assist chat came to; the wire ``reason`` of a failed one."""

    STARTED = auto()
    TOO_OLD = auto()
    """The workspace predates the assist skill; nothing was created."""
    START_FAILED = auto()
    """The launch could not reach the workspace, and the workspace would not start."""
    UNREACHABLE = auto()
    """The launch could not reach the workspace, even after starting it (or with no way to start it)."""
    SPAWN_FAILED = auto()
    """The workspace had the skill, but the chat's create did not land."""


class AssistChatLaunch(FrozenModel):
    """What an assist launch came to, and what the workspace, mngr or the start said when it failed."""

    outcome: AssistChatOutcome = Field(description="What the launch came to")
    failure_detail: str = Field(description="The workspace's, mngr's or the start's own words; '' when none")


def launch_assist_chat_starting_its_workspace(
    launch_chat: Callable[[], SkillChatLaunch],
    start_workspace: Callable[[], MindHostActionOutcome] | None,
) -> AssistChatLaunch:
    """Launch the assist chat, starting the workspace and launching again when the first launch could not reach it.

    The same order as an update's dispatch: the launch's own reach is the evidence that the
    workspace is down, and a start against a running workspace would cost as much as the launch.
    ``start_workspace`` is None where this app cannot start workspaces at all.
    """
    launch = launch_chat()
    if launch.outcome is SkillChatLaunchOutcome.UNREACHABLE and start_workspace is not None:
        start = start_workspace()
        if not start.is_successful:
            return AssistChatLaunch(
                outcome=AssistChatOutcome.START_FAILED,
                failure_detail=(start.failure_reason or "")[:FAILURE_DETAIL_MAX_CHARS],
            )
        launch = launch_chat()
    match launch.outcome:
        case SkillChatLaunchOutcome.STARTED:
            outcome = AssistChatOutcome.STARTED
        case SkillChatLaunchOutcome.UNSUPPORTED:
            outcome = AssistChatOutcome.TOO_OLD
        case SkillChatLaunchOutcome.UNREACHABLE:
            outcome = AssistChatOutcome.UNREACHABLE
        case SkillChatLaunchOutcome.SPAWN_FAILED:
            outcome = AssistChatOutcome.SPAWN_FAILED
        case _ as unreachable:
            assert_never(unreachable)
    return AssistChatLaunch(outcome=outcome, failure_detail=launch.failure_detail)
