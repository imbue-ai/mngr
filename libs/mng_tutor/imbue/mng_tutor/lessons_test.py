from imbue.mng_tutor.data_types import AgentExistsCheck
from imbue.mng_tutor.data_types import AgentNotExistsCheck
from imbue.mng_tutor.lessons import ALL_LESSONS
from imbue.mng_tutor.lessons import LESSON_GETTING_STARTED
from imbue.mng_tutor.lessons import LESSON_REMOTE_AGENTS


def test_all_lessons_tuple_contains_all_defined_lessons() -> None:
    assert len(ALL_LESSONS) == 2
    assert ALL_LESSONS[0] is LESSON_GETTING_STARTED
    assert ALL_LESSONS[1] is LESSON_REMOTE_AGENTS


def test_getting_started_lesson_has_expected_structure() -> None:
    assert LESSON_GETTING_STARTED.title == "Basic Local Agent"
    assert len(LESSON_GETTING_STARTED.steps) == 5

    # First step creates an agent
    assert isinstance(LESSON_GETTING_STARTED.steps[0].check, AgentExistsCheck)

    # Last step destroys the agent
    assert isinstance(LESSON_GETTING_STARTED.steps[4].check, AgentNotExistsCheck)


def test_remote_agents_lesson_has_expected_structure() -> None:
    assert LESSON_REMOTE_AGENTS.title == "Remote Agents on Modal (WIP)"
    assert len(LESSON_REMOTE_AGENTS.steps) == 5

    # First step creates a remote agent
    assert isinstance(LESSON_REMOTE_AGENTS.steps[0].check, AgentExistsCheck)

    # Last step destroys the agent
    assert isinstance(LESSON_REMOTE_AGENTS.steps[4].check, AgentNotExistsCheck)


def test_all_lessons_have_non_empty_steps() -> None:
    for lesson in ALL_LESSONS:
        assert len(lesson.steps) > 0
        for step in lesson.steps:
            assert len(step.heading) > 0
            assert len(step.details) > 0


def test_all_lessons_have_title_and_description() -> None:
    for lesson in ALL_LESSONS:
        assert len(lesson.title) > 0
        assert len(lesson.description) > 0
