from typing import Any

import click

from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng_tutor.lessons import ALL_LESSONS
from imbue.mng_tutor.tui import run_lesson_runner
from imbue.mng_tutor.tui import run_lesson_selector


class TutorCliOptions(CommonCliOptions):
    """Options for the tutor command."""


@click.command()
@add_common_options
@click.pass_context
def tutor(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="tutor",
        command_class=TutorCliOptions,
    )

    # Loop: select a lesson, run it, return to selector when done
    lesson = run_lesson_selector(ALL_LESSONS)
    while lesson is not None:  # pragma: no cover
        run_lesson_runner(lesson, mng_ctx)
        lesson = run_lesson_selector(ALL_LESSONS)


CommandHelpMetadata(
    key="tutor",
    one_line_description="Interactive tutorial for learning mng commands",
    synopsis="mng tutor [OPTIONS]",
    description="""Launches an interactive tutorial that guides you through learning
mng commands step by step. Run this in a separate terminal from your
main working terminal.

Each lesson contains a series of steps with automatic completion detection.
Follow the instructions in the tutor terminal and complete each step in
your other terminal. The tutor automatically detects when each step is
complete and advances to the next one.""",
    examples=(("Start the interactive tutor", "mng tutor"),),
    see_also=(
        ("create", "Create a new agent"),
        ("connect", "Connect to an agent"),
        ("list", "List agents"),
    ),
).register()

add_pager_help_option(tutor)
