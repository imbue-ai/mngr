from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr_tutor.data_types import AgentExistsCheck
from imbue.mngr_tutor.data_types import AgentInStateCheck
from imbue.mngr_tutor.data_types import AgentNotExistsCheck
from imbue.mngr_tutor.data_types import FileExistsInAgentWorkDirCheck
from imbue.mngr_tutor.data_types import Lesson
from imbue.mngr_tutor.data_types import LessonStep
from imbue.mngr_tutor.data_types import TmuxSessionHasClientsCheck


def test_agent_exists_check_construction() -> None:
    check = AgentExistsCheck(agent_name=AgentName("test-agent"))
    assert check.check_type == "agent_exists"
    assert check.agent_name == AgentName("test-agent")


def test_agent_not_exists_check_construction() -> None:
    check = AgentNotExistsCheck(agent_name=AgentName("test-agent"))
    assert check.check_type == "agent_not_exists"
    assert check.agent_name == AgentName("test-agent")


def test_agent_in_state_check_construction() -> None:
    check = AgentInStateCheck(
        agent_name=AgentName("test-agent"),
        expected_states=(AgentLifecycleState.RUNNING,),
    )
    assert check.check_type == "agent_in_state"
    assert check.agent_name == AgentName("test-agent")
    assert check.expected_states == (AgentLifecycleState.RUNNING,)


def test_agent_in_state_check_accepts_multiple_states() -> None:
    check = AgentInStateCheck(
        agent_name=AgentName("test-agent"),
        expected_states=(AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING),
    )
    assert len(check.expected_states) == 2


def test_file_exists_check_construction() -> None:
    check = FileExistsInAgentWorkDirCheck(
        agent_name=AgentName("test-agent"),
        file_path="hello.txt",
    )
    assert check.check_type == "file_exists_in_work_dir"
    assert check.agent_name == AgentName("test-agent")
    assert check.file_path == "hello.txt"


def test_tmux_session_has_clients_check_construction() -> None:
    check = TmuxSessionHasClientsCheck(agent_name=AgentName("test-agent"))
    assert check.check_type == "tmux_session_has_clients"
    assert check.agent_name == AgentName("test-agent")


def test_lesson_step_with_agent_exists_check() -> None:
    step = LessonStep(
        heading="Create an agent",
        details="Run `mngr create test-agent`.",
        check=AgentExistsCheck(agent_name=AgentName("test-agent")),
    )
    assert step.heading == "Create an agent"
    assert isinstance(step.check, AgentExistsCheck)


def test_lesson_step_with_agent_in_state_check() -> None:
    step = LessonStep(
        heading="Stop the agent",
        details="Run `mngr stop test-agent`.",
        check=AgentInStateCheck(
            agent_name=AgentName("test-agent"),
            expected_states=(AgentLifecycleState.STOPPED,),
        ),
    )
    assert isinstance(step.check, AgentInStateCheck)
    assert step.check.expected_states == (AgentLifecycleState.STOPPED,)


def test_lesson_construction() -> None:
    lesson = Lesson(
        title="Test Lesson",
        description="A test lesson.",
        steps=(
            LessonStep(
                heading="Step 1",
                details="Do step 1.",
                check=AgentExistsCheck(agent_name=AgentName("agent-1")),
            ),
            LessonStep(
                heading="Step 2",
                details="Do step 2.",
                check=AgentNotExistsCheck(agent_name=AgentName("agent-1")),
            ),
        ),
    )
    assert lesson.title == "Test Lesson"
    assert len(lesson.steps) == 2
    assert isinstance(lesson.steps[0].check, AgentExistsCheck)
    assert isinstance(lesson.steps[1].check, AgentNotExistsCheck)
