import pytest

from imbue.mngr_tutor.data_types import AgentExistsCheck
from imbue.mngr_tutor.data_types import AgentNotExistsCheck
from imbue.mngr_tutor.data_types import Lesson
from imbue.mngr_tutor.lessons import ALL_LESSONS
from imbue.mngr_tutor.lessons import LESSON_GETTING_STARTED
from imbue.mngr_tutor.lessons import LESSON_REMOTE_AGENTS


def test_all_lessons_tuple_contains_the_defined_lessons_in_order() -> None:
    assert ALL_LESSONS == (LESSON_GETTING_STARTED, LESSON_REMOTE_AGENTS)


@pytest.mark.parametrize("lesson", ALL_LESSONS, ids=lambda lesson: lesson.title)
def test_lesson_has_non_blank_title_description_and_step_text(lesson: Lesson) -> None:
    """Every lesson and step carries real (non-whitespace) user-facing text.

    Guards against an authoring slip that leaves a heading/detail blank or whitespace-only,
    which `len(...) > 0` would miss.
    """
    assert lesson.title.strip()
    assert lesson.description.strip()
    assert len(lesson.steps) > 0
    for step in lesson.steps:
        assert step.heading.strip()
        assert step.details.strip()


@pytest.mark.parametrize("lesson", ALL_LESSONS, ids=lambda lesson: lesson.title)
def test_lesson_starts_by_creating_and_ends_by_destroying_one_agent(lesson: Lesson) -> None:
    """Each lesson's narrative arc is create-the-agent first, destroy-it last, and every
    step in between operates on that same agent.

    This catches a real authoring bug: a step whose check references the wrong agent name
    (e.g. a copy-paste from the other lesson), which would make the tutor never detect
    completion of that step.
    """
    first_check = lesson.steps[0].check
    last_check = lesson.steps[-1].check

    assert isinstance(first_check, AgentExistsCheck)
    assert isinstance(last_check, AgentNotExistsCheck)

    agent_name = first_check.agent_name
    assert last_check.agent_name == agent_name
    for step in lesson.steps:
        assert step.check.agent_name == agent_name
