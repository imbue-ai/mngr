"""Unit tests for the tutor CLI command."""

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr_tutor.cli import tutor
from imbue.mngr_tutor.data_types import Lesson
from imbue.mngr_tutor.lessons import ALL_LESSONS


def test_tutor_command_invokes_lesson_selector_with_all_lessons(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tutor command should call run_lesson_selector with ALL_LESSONS.

    run_lesson_selector launches a real urwid TUI, so it is replaced with a recorder
    that captures its argument and returns None (which exits the selector loop). The
    assertion checks the lessons actually handed to the selector, so passing the wrong
    tuple (or dropping the call) would fail -- unlike asserting only the exit code.
    """
    received_lessons: list[tuple[Lesson, ...]] = []

    def _record_and_exit(lessons: tuple[Lesson, ...]) -> None:
        received_lessons.append(lessons)
        return None

    monkeypatch.setattr("imbue.mngr_tutor.cli.run_lesson_selector", _record_and_exit)

    result = cli_runner.invoke(tutor, [], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0
    assert received_lessons == [ALL_LESSONS]
