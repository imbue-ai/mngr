import pytest

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr_tutor.data_types import AgentExistsCheck
from imbue.mngr_tutor.data_types import AgentInStateCheck
from imbue.mngr_tutor.data_types import AgentNotExistsCheck
from imbue.mngr_tutor.data_types import FileExistsInAgentWorkDirCheck
from imbue.mngr_tutor.data_types import Lesson
from imbue.mngr_tutor.data_types import LessonStep
from imbue.mngr_tutor.data_types import StepCheck
from imbue.mngr_tutor.data_types import TmuxSessionHasClientsCheck

# One instance of every member of the StepCheck union. The check_type literals
# these carry are the contract the StepCheck Discriminator dispatches on, so the
# round-trip tests below cover all five branches.
ALL_CHECKS: tuple[StepCheck, ...] = (
    AgentExistsCheck(agent_name=AgentName("agent-smith")),
    AgentNotExistsCheck(agent_name=AgentName("agent-smith")),
    AgentInStateCheck(
        agent_name=AgentName("agent-smith"),
        expected_states=(AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING),
    ),
    FileExistsInAgentWorkDirCheck(agent_name=AgentName("agent-smith"), file_path="hello.txt"),
    TmuxSessionHasClientsCheck(agent_name=AgentName("agent-smith")),
)


@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda c: c.check_type)
def test_lesson_step_round_trips_through_serialization_preserving_check_subtype(check: StepCheck) -> None:
    """A LessonStep survives model_dump/model_validate with the StepCheck Discriminator
    reconstructing the exact check subtype.

    This is the only non-trivial behavior of these models. A wrong or duplicated
    check_type literal (data_types.py), or a union member dropped from StepCheck, would
    make the discriminator route to the wrong type (or fail), and this would catch it.
    """
    step = LessonStep(heading="Heading", details="Details", check=check)

    reconstructed = LessonStep.model_validate(step.model_dump())

    assert type(reconstructed.check) is type(check)
    assert reconstructed == step


def test_step_check_discriminator_dispatches_on_check_type_from_raw_dict() -> None:
    """Validating a raw dict (as if loaded from persisted/transmitted data) routes to
    the subtype named by its check_type field, not whatever was built in Python."""
    step = LessonStep.model_validate(
        {
            "heading": "Stop the agent",
            "details": "Run mngr stop agent-smith.",
            "check": {
                "check_type": "agent_in_state",
                "agent_name": "agent-smith",
                "expected_states": ["STOPPED"],
            },
        }
    )

    assert isinstance(step.check, AgentInStateCheck)
    assert step.check.agent_name == AgentName("agent-smith")
    assert step.check.expected_states == (AgentLifecycleState.STOPPED,)


def test_step_check_validation_rejects_unknown_check_type() -> None:
    """The discriminated union must reject a check_type that is not one of its members,
    rather than silently constructing some default."""
    with pytest.raises(ValueError):
        LessonStep.model_validate(
            {
                "heading": "Heading",
                "details": "Details",
                "check": {"check_type": "not_a_real_check", "agent_name": "agent-smith"},
            }
        )


def test_check_type_literals_are_unique_across_the_step_check_union() -> None:
    """The Discriminator requires distinct check_type values across the union; a
    copy-paste duplicate would make dispatch ambiguous."""
    check_types = [check.check_type for check in ALL_CHECKS]

    assert sorted(check_types) == sorted(set(check_types))


def test_lesson_construction_preserves_ordered_steps() -> None:
    """Lesson keeps its steps in the order given (steps are ordered by contract)."""
    first_step = LessonStep(
        heading="Step 1",
        details="Do step 1.",
        check=AgentExistsCheck(agent_name=AgentName("agent-1")),
    )
    second_step = LessonStep(
        heading="Step 2",
        details="Do step 2.",
        check=AgentNotExistsCheck(agent_name=AgentName("agent-1")),
    )

    lesson = Lesson(title="Test Lesson", description="A test lesson.", steps=(first_step, second_step))

    assert lesson.steps == (first_step, second_step)
