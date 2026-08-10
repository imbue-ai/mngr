"""Unit tests for the tutor TUI."""

from types import SimpleNamespace
from typing import Any

import pytest
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.filler import Filler
from urwid.widget.listbox import SimpleFocusListWalker
from urwid.widget.pile import Pile
from urwid.widget.text import Text
from urwid.widget.wimp import SelectableIcon

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentName
from imbue.mngr_tutor.data_types import AgentExistsCheck
from imbue.mngr_tutor.data_types import AgentNotExistsCheck
from imbue.mngr_tutor.data_types import Lesson
from imbue.mngr_tutor.data_types import LessonStep
from imbue.mngr_tutor.tui import CHECK_INTERVAL_SECONDS
from imbue.mngr_tutor.tui import _LessonRunnerInputHandler
from imbue.mngr_tutor.tui import _LessonRunnerState
from imbue.mngr_tutor.tui import _LessonSelectorInputHandler
from imbue.mngr_tutor.tui import _LessonSelectorState
from imbue.mngr_tutor.tui import _build_step_widgets
from imbue.mngr_tutor.tui import _get_current_step_index
from imbue.mngr_tutor.tui import _on_check_alarm
from imbue.mngr_tutor.tui import _refresh_display
from imbue.mngr_tutor.tui import _schedule_next_check

# =============================================================================
# Helpers
# =============================================================================


class _AlarmRecorder:
    """Records every call to a loop's set_alarm_in so tests can assert the exact
    delay, callback, and arguments an alarm was scheduled with (not just the count)."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> None:
        self.calls.append(args)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _make_mock_loop() -> Any:
    """Create a lightweight loop substitute with a recording set_alarm_in.

    A real urwid MainLoop opens a screen and an event loop, neither of which is
    needed to test scheduling, so this records the set_alarm_in calls instead.
    """
    recorder = _AlarmRecorder()
    return SimpleNamespace(set_alarm_in=recorder, _alarm_recorder=recorder)


def _collect_text(widget: object) -> str:
    """Recursively join the rendered text of a Text/Pile/Filler widget tree."""
    if isinstance(widget, Text):
        return str(widget.get_text()[0])
    if isinstance(widget, Filler):
        return _collect_text(widget.original_widget)
    if isinstance(widget, Pile):
        return " ".join(_collect_text(child) for child, _ in widget.contents)
    return ""


def _make_step(heading: str = "Step", details: str = "Do something") -> LessonStep:
    return LessonStep(
        heading=heading,
        details=details,
        check=AgentExistsCheck(agent_name=AgentName("test-agent")),
    )


def _make_lesson(
    title: str = "Test Lesson",
    description: str = "A test lesson",
    steps: tuple[LessonStep, ...] | None = None,
) -> Lesson:
    if steps is None:
        steps = (_make_step("Step 1", "First step"), _make_step("Step 2", "Second step"))
    return Lesson(title=title, description=description, steps=steps)


def _make_runner_state(
    lesson: Lesson | None = None,
    step_completed: list[bool] | None = None,
) -> _LessonRunnerState:
    if lesson is None:
        lesson = _make_lesson()
    if step_completed is None:
        step_completed = [False] * len(lesson.steps)
    frame = SimpleNamespace(body=None)
    status_text = Text("")
    # These widget/scheduling tests never read mngr_ctx (the check is only run via
    # _on_check_alarm, which has its own real-context helper below), so we bypass
    # validation with model_construct rather than spin up a real MngrContext.
    mngr_ctx = SimpleNamespace()
    return _LessonRunnerState.model_construct(
        lesson=lesson,
        mngr_ctx=mngr_ctx,
        step_completed=step_completed,
        frame=frame,
        status_text=status_text,
    )


def _make_selector_handler() -> tuple[_LessonSelectorInputHandler, _LessonSelectorState]:
    """Create a selector handler and its state for testing."""
    lessons = (_make_lesson(),)
    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker([])
    state = _LessonSelectorState(lessons=lessons, list_walker=list_walker)
    handler = _LessonSelectorInputHandler(state=state)
    return handler, state


# =============================================================================
# Tests for _get_current_step_index
# =============================================================================


def test_get_current_step_index_all_incomplete() -> None:
    assert _get_current_step_index([False, False, False]) == 0


def test_get_current_step_index_first_complete() -> None:
    assert _get_current_step_index([True, False, False]) == 1


def test_get_current_step_index_all_complete() -> None:
    assert _get_current_step_index([True, True, True]) is None


def test_get_current_step_index_middle_incomplete() -> None:
    assert _get_current_step_index([True, False, True]) == 1


def test_get_current_step_index_empty_list() -> None:
    assert _get_current_step_index([]) is None


# =============================================================================
# Tests for _build_step_widgets
# =============================================================================


def test_build_step_widgets_shows_all_steps() -> None:
    state = _make_runner_state()
    widgets = _build_step_widgets(state)
    text_content = " ".join(str(w.get_text()[0]) for w in widgets if isinstance(w, Text))
    assert "Step 1" in text_content
    assert "Step 2" in text_content


def test_build_step_widgets_current_step_shows_details() -> None:
    state = _make_runner_state()
    widgets = _build_step_widgets(state)
    text_content = " ".join(str(w.get_text()[0]) for w in widgets if isinstance(w, Text))
    assert "First step" in text_content
    assert "Second step" not in text_content


def test_build_step_widgets_completed_step_shows_checkmark() -> None:
    state = _make_runner_state(step_completed=[True, False])
    widgets = _build_step_widgets(state)
    text_content = " ".join(str(w.get_text()[0]) for w in widgets if isinstance(w, Text))
    assert "[x]" in text_content
    assert "[ ]" in text_content


def test_build_step_widgets_all_complete_shows_message() -> None:
    state = _make_runner_state(step_completed=[True, True])
    widgets = _build_step_widgets(state)
    text_content = " ".join(str(w.get_text()[0]) for w in widgets if isinstance(w, Text))
    assert "Lesson complete!" in text_content


def test_build_step_widgets_all_complete_has_no_details() -> None:
    state = _make_runner_state(step_completed=[True, True])
    widgets = _build_step_widgets(state)
    text_content = " ".join(str(w.get_text()[0]) for w in widgets if isinstance(w, Text))
    assert "First step" not in text_content
    assert "Second step" not in text_content


# =============================================================================
# Tests for _refresh_display
# =============================================================================


def test_refresh_display_populates_frame_body_with_current_step_content() -> None:
    state = _make_runner_state()
    _refresh_display(state)
    assert isinstance(state.frame.body, Filler)
    text_content = _collect_text(state.frame.body)
    assert "Step 1" in text_content
    assert "First step" in text_content


# =============================================================================
# Tests for _LessonSelectorInputHandler
# =============================================================================


def test_selector_input_handler_q_exits() -> None:
    handler, _ = _make_selector_handler()
    with pytest.raises(ExitMainLoop):
        handler("q")


def test_selector_input_handler_ctrl_c_exits() -> None:
    handler, _ = _make_selector_handler()
    with pytest.raises(ExitMainLoop):
        handler("ctrl c")


def test_selector_input_handler_enter_sets_result_index() -> None:
    lessons = (_make_lesson(title="L1"), _make_lesson(title="L2"))
    items = [
        AttrMap(SelectableIcon(f"  {idx + 1}. {lesson.title}", cursor_position=0), None)
        for idx, lesson in enumerate(lessons)
    ]
    list_walker = SimpleFocusListWalker(items)
    state = _LessonSelectorState(lessons=lessons, list_walker=list_walker)
    handler = _LessonSelectorInputHandler(state=state)

    with pytest.raises(ExitMainLoop):
        handler("enter")
    assert state.result_index == 0


def test_selector_input_handler_arrow_keys_pass_through() -> None:
    handler, _ = _make_selector_handler()
    for key in ("up", "down", "page up", "page down", "home", "end"):
        assert handler(key) is None


def test_selector_input_handler_ignores_mouse_events() -> None:
    handler, state = _make_selector_handler()
    result = handler(("mouse press", 1, 0, 0))
    assert result is None
    assert state.result_index is None


def test_selector_input_handler_swallows_other_keys() -> None:
    handler, state = _make_selector_handler()
    result = handler("x")
    assert result is True
    assert state.result_index is None


# =============================================================================
# Tests for _LessonRunnerInputHandler
# =============================================================================


def test_runner_input_handler_q_exits() -> None:
    with pytest.raises(ExitMainLoop):
        _LessonRunnerInputHandler()("q")


def test_runner_input_handler_uppercase_q_exits() -> None:
    with pytest.raises(ExitMainLoop):
        _LessonRunnerInputHandler()("Q")


def test_runner_input_handler_ctrl_c_exits() -> None:
    with pytest.raises(ExitMainLoop):
        _LessonRunnerInputHandler()("ctrl c")


def test_runner_input_handler_ignores_mouse_events() -> None:
    assert _LessonRunnerInputHandler()(("mouse press", 1, 0, 0)) is None


def test_runner_input_handler_swallows_other_keys() -> None:
    assert _LessonRunnerInputHandler()("x") is True


# =============================================================================
# Tests for _schedule_next_check and _on_check_alarm
# =============================================================================


def test_schedule_next_check_schedules_on_check_alarm_at_the_check_interval() -> None:
    loop = _make_mock_loop()
    state = _make_runner_state()
    _schedule_next_check(loop, state)
    assert loop._alarm_recorder.calls == [(CHECK_INTERVAL_SECONDS, _on_check_alarm, state)]


def test_on_check_alarm_all_complete_sets_status() -> None:
    state = _make_runner_state(step_completed=[True, True])
    _on_check_alarm(_make_mock_loop(), state)
    assert "complete" in str(state.status_text.get_text()[0]).lower()


def _make_runner_state_with_ctx(
    mngr_ctx: MngrContext,
    step_completed: list[bool] | None = None,
    passing_check: bool = False,
) -> _LessonRunnerState:
    """Create a runner state with a real MngrContext and checks that naturally pass/fail."""
    check_agent = AgentName("nonexistent-test-agent")
    if passing_check:
        check = AgentNotExistsCheck(agent_name=check_agent)
    else:
        check = AgentExistsCheck(agent_name=check_agent)
    steps = (
        LessonStep(heading="Step 1", details="First", check=check),
        LessonStep(heading="Step 2", details="Second", check=check),
    )
    lesson = Lesson(title="Test", description="Test", steps=steps)
    if step_completed is None:
        step_completed = [False] * len(lesson.steps)
    frame = SimpleNamespace(body=None)
    status_text = Text("")
    # A real MngrContext is available here, so use the validating constructor.
    return _LessonRunnerState(
        lesson=lesson,
        mngr_ctx=mngr_ctx,
        step_completed=step_completed,
        frame=frame,
        status_text=status_text,
    )


def test_on_check_alarm_step_not_passed_schedules_next(temp_mngr_ctx: MngrContext) -> None:
    state = _make_runner_state_with_ctx(temp_mngr_ctx, step_completed=[False, False], passing_check=False)
    loop = _make_mock_loop()
    _on_check_alarm(loop, state)
    assert loop._alarm_recorder.call_count == 1
    assert state.step_completed[0] is False


def test_on_check_alarm_step_passed_advances(temp_mngr_ctx: MngrContext) -> None:
    state = _make_runner_state_with_ctx(temp_mngr_ctx, step_completed=[False, False], passing_check=True)
    loop = _make_mock_loop()
    _on_check_alarm(loop, state)
    assert state.step_completed[0] is True
    assert loop._alarm_recorder.call_count == 1


def test_on_check_alarm_last_step_passed_shows_complete(temp_mngr_ctx: MngrContext) -> None:
    state = _make_runner_state_with_ctx(temp_mngr_ctx, step_completed=[True, False], passing_check=True)
    loop = _make_mock_loop()
    _on_check_alarm(loop, state)
    assert state.step_completed[1] is True
    assert "complete" in str(state.status_text.get_text()[0]).lower()
