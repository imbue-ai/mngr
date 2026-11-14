from collections.abc import Callable

from imbue.minds.desktop_client.assist_chat import AssistChatOutcome
from imbue.minds.desktop_client.assist_chat import build_assist_chat_message
from imbue.minds.desktop_client.assist_chat import launch_assist_chat_starting_its_workspace
from imbue.minds.desktop_client.skill_chat import SkillChatLaunch
from imbue.minds.desktop_client.skill_chat import SkillChatLaunchOutcome
from imbue.minds.desktop_client.workspace_lifecycle import MindHostActionOutcome


def test_the_description_rides_the_assist_slash_command_verbatim() -> None:
    assert build_assist_chat_message("the database migration failed") == "/assist the database migration failed"


def _launcher(*outcomes: SkillChatLaunch) -> tuple[list[SkillChatLaunch], Callable[[], SkillChatLaunch]]:
    """A launch that answers ``outcomes`` in turn, and the list of what it has answered so far."""
    answered: list[SkillChatLaunch] = []
    remaining = list(outcomes)

    def launch() -> SkillChatLaunch:
        answered.append(remaining.pop(0))
        return answered[-1]

    return answered, launch


_UNREACHABLE = SkillChatLaunch(outcome=SkillChatLaunchOutcome.UNREACHABLE, failure_detail="ssh: connection refused")
_STARTED = SkillChatLaunch(outcome=SkillChatLaunchOutcome.STARTED)


def test_a_workspace_the_launch_cannot_reach_is_started_and_the_launch_runs_again() -> None:
    answered, launch = _launcher(_UNREACHABLE, _STARTED)
    starts: list[str] = []

    def start() -> MindHostActionOutcome:
        starts.append("start")
        return MindHostActionOutcome(is_successful=True)

    result = launch_assist_chat_starting_its_workspace(launch_chat=launch, start_workspace=start)

    assert result.outcome is AssistChatOutcome.STARTED
    assert len(starts) == 1
    assert len(answered) == 2


def test_a_workspace_that_will_not_start_reports_the_starts_own_reason_and_launches_nothing_more() -> None:
    answered, launch = _launcher(_UNREACHABLE)

    result = launch_assist_chat_starting_its_workspace(
        launch_chat=launch,
        start_workspace=lambda: MindHostActionOutcome(is_successful=False, failure_reason="ERROR: quota exceeded"),
    )

    assert result.outcome is AssistChatOutcome.START_FAILED
    assert result.failure_detail == "ERROR: quota exceeded"
    assert len(answered) == 1


def test_a_reachable_workspace_is_never_started() -> None:
    too_old = SkillChatLaunch(outcome=SkillChatLaunchOutcome.UNSUPPORTED)
    _, launch = _launcher(too_old)
    starts: list[str] = []

    def start() -> MindHostActionOutcome:
        starts.append("start")
        return MindHostActionOutcome(is_successful=True)

    result = launch_assist_chat_starting_its_workspace(launch_chat=launch, start_workspace=start)

    assert result.outcome is AssistChatOutcome.TOO_OLD
    assert starts == []


def test_an_unreachable_workspace_keeps_mngrs_account_when_nothing_can_start_it() -> None:
    _, launch = _launcher(_UNREACHABLE)

    result = launch_assist_chat_starting_its_workspace(launch_chat=launch, start_workspace=None)

    assert result.outcome is AssistChatOutcome.UNREACHABLE
    assert result.failure_detail == "ssh: connection refused"
