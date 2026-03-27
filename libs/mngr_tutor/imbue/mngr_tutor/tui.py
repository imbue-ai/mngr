from textwrap import dedent
from typing import Any

from loguru import logger
from pydantic import ConfigDict
from urwid.display.raw import Screen
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.event_loop.main_loop import MainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.divider import Divider
from urwid.widget.filler import Filler
from urwid.widget.frame import Frame
from urwid.widget.listbox import ListBox
from urwid.widget.listbox import SimpleFocusListWalker
from urwid.widget.pile import Pile
from urwid.widget.text import Text
from urwid.widget.wimp import SelectableIcon

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_tutor.checks import run_check
from imbue.mngr_tutor.data_types import Lesson

PALETTE = [
    ("header", "white", "dark blue"),
    ("status", "white", "dark blue"),
    ("reversed", "standout", ""),
    ("completed", "dark green", ""),
    ("current_heading", "bold", ""),
]

CHECK_INTERVAL_SECONDS: int = 3


# === Lesson Selector ===


class _LessonSelectorState(MutableModel):
    """Mutable state for the lesson selector UI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    lessons: tuple[Lesson, ...]
    list_walker: Any
    result_index: int | None = None


class _LessonSelectorInputHandler(MutableModel):
    """Callable input handler for the lesson selector."""

    state: _LessonSelectorState

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:
        """Handle keyboard input. Returns True if handled, None to pass through."""
        if isinstance(key, tuple):
            return None
        if key in ("q", "ctrl c"):
            raise ExitMainLoop()
        if key == "enter":
            if self.state.list_walker:
                _, focus_index = self.state.list_walker.get_focus()
                if focus_index is not None:
                    self.state.result_index = focus_index
            raise ExitMainLoop()
        # Let arrow keys pass through to the ListBox
        if key in ("up", "down", "page up", "page down", "home", "end"):
            return None
        return True


def run_lesson_selector(lessons: tuple[Lesson, ...]) -> Lesson | None:  # pragma: no cover
    """Run the lesson selector TUI. Returns the selected lesson or None if cancelled."""
    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker([])

    for idx, lesson in enumerate(lessons):
        text = f"  {idx + 1}. {lesson.title}\n     {lesson.description}"
        item = SelectableIcon(text, cursor_position=0)
        list_walker.append(AttrMap(item, None, focus_map="reversed"))

    state = _LessonSelectorState(lessons=lessons, list_walker=list_walker)

    header = Pile(
        [
            AttrMap(Text("mngr Tutor - Select a Lesson", align="center"), "header"),
            Divider(),
            Text(
                dedent("""\
                Welcome to mngr! Now let's learn how mngr works!

                Open another terminal window to run mngr commands,
                and select a lesson below.

                (To tmux users: mngr itself uses tmux, so to keep things simple,
                we suggest that you also open a separate terminal window for now.
                Running the tutor itself in tmux is fine, though.)""")
            ),
            Divider(),
        ]
    )

    footer = Pile(
        [
            Divider(),
            AttrMap(Text("  Up/Down to navigate, Enter to select, q to quit"), "status"),
        ]
    )

    listbox = ListBox(list_walker)
    frame = Frame(body=listbox, header=header, footer=footer)

    input_handler = _LessonSelectorInputHandler(state=state)

    screen = Screen()
    screen.tty_signal_keys(intr="undefined")

    loop = MainLoop(frame, palette=PALETTE, unhandled_input=input_handler, screen=screen)
    loop.run()

    if state.result_index is not None:
        return lessons[state.result_index]
    return None


# === Lesson Runner ===


class _LessonRunnerState(MutableModel):
    """Mutable state for the lesson runner UI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    lesson: Lesson
    mngr_ctx: MngrContext
    step_completed: list[bool]
    frame: Any
    status_text: Any


class _LessonRunnerInputHandler(MutableModel):
    """Callable input handler for the lesson runner."""

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:
        """Handle keyboard input. Returns True if handled, None to pass through."""
        if isinstance(key, tuple):
            return None
        if key in ("q", "Q", "ctrl c"):
            raise ExitMainLoop()
        return True


def _get_current_step_index(step_completed: list[bool]) -> int | None:
    """Get the index of the first incomplete step, or None if all complete."""
    for idx, completed in enumerate(step_completed):
        if not completed:
            return idx
    return None


def _build_step_widgets(state: _LessonRunnerState) -> list[Text | Divider]:
    """Build the list of widgets for the step display."""
    widgets: list[Text | Divider] = []
    current_step_index = _get_current_step_index(state.step_completed)

    for idx, step in enumerate(state.lesson.steps):
        is_completed = state.step_completed[idx]
        is_current = idx == current_step_index

        # Step heading with check mark
        if is_completed:
            mark = "[x]"
            attr = "completed"
        elif is_current:
            mark = "[ ]"
            attr = "current_heading"
        else:
            mark = "[ ]"
            attr = None

        heading = f"  {mark} {idx + 1}. {step.heading}"
        widgets.append(Text((attr, heading)) if attr is not None else Text(heading))

        # Show details for the current step
        if is_current:
            widgets.append(Text(""))
            for line in step.details.split("\n"):
                widgets.append(Text(f"        {line}"))
            widgets.append(Text(""))

    # Show completion message if all steps are done
    if current_step_index is None:
        widgets.append(Divider())
        widgets.append(Text(("completed", "  Lesson complete!")))

    return widgets


def _refresh_display(state: _LessonRunnerState) -> None:
    """Rebuild the body display with current step statuses."""
    widgets = _build_step_widgets(state)
    body_pile = Pile(widgets)
    state.frame.body = Filler(body_pile, valign="top")


def _schedule_next_check(loop: MainLoop, state: _LessonRunnerState) -> None:
    """Schedule the next check alarm."""
    loop.set_alarm_in(CHECK_INTERVAL_SECONDS, _on_check_alarm, state)


def _on_check_alarm(loop: MainLoop, state: _LessonRunnerState) -> None:
    """Alarm callback that runs the check for the current step."""
    current_idx = _get_current_step_index(state.step_completed)
    if current_idx is None:
        # All steps already complete
        state.status_text.set_text("  Lesson complete! Press q to go back.")
        return

    # Run the check for the current step
    step = state.lesson.steps[current_idx]
    is_passed = run_check(step.check, state.mngr_ctx)

    if is_passed:
        state.step_completed[current_idx] = True
        _refresh_display(state)

    # Schedule the next check if there are remaining steps
    new_current = _get_current_step_index(state.step_completed)
    if new_current is not None:
        state.status_text.set_text(f"  Checking step {new_current + 1}/{len(state.lesson.steps)}... (q to go back)")
        _schedule_next_check(loop, state)
    else:
        state.status_text.set_text("  Lesson complete! Press q to go back.")
        _refresh_display(state)


def run_lesson_runner(lesson: Lesson, mngr_ctx: MngrContext) -> None:  # pragma: no cover
    """Run the lesson runner TUI with periodic check polling."""
    status_text = Text("  Starting lesson... (q to go back)")
    status_bar = AttrMap(status_text, "status")

    # Initial empty body (will be populated by _refresh_display)
    initial_body = Filler(Pile([Text("")]), valign="top")

    header = Pile(
        [
            AttrMap(Text(f"mngr Tutor - {lesson.title}", align="center"), "header"),
            Divider(),
        ]
    )

    footer = Pile(
        [
            Divider(),
            status_bar,
        ]
    )

    frame = Frame(body=initial_body, header=header, footer=footer)

    state = _LessonRunnerState(
        lesson=lesson,
        mngr_ctx=mngr_ctx,
        step_completed=[False] * len(lesson.steps),
        frame=frame,
        status_text=status_text,
    )

    # Build initial display
    _refresh_display(state)

    input_handler = _LessonRunnerInputHandler()

    screen = Screen()
    screen.tty_signal_keys(intr="undefined")

    loop = MainLoop(frame, palette=PALETTE, unhandled_input=input_handler, screen=screen)

    # Schedule the first check
    _schedule_next_check(loop, state)

    # Suppress logging while the TUI is running to avoid display corruption
    logger.disable("imbue")
    try:
        loop.run()
    finally:
        logger.enable("imbue")
