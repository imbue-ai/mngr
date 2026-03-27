from typing import Any

import click
from click_option_group import optgroup

from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng_schedule.cli.group import schedule
from imbue.mng_schedule.cli.options import ScheduleRemoveCliOptions


@schedule.command(name="remove")
@click.argument("names", nargs=-1, required=True)
@optgroup.group("Safety")
@optgroup.option(
    "-f",
    "--force",
    is_flag=True,
    help="Skip confirmation prompt.",
)
@add_common_options
@click.pass_context
def schedule_remove(ctx: click.Context, **kwargs: Any) -> None:
    """Remove one or more scheduled triggers.

    \b
    Examples:
      mng schedule remove my-trigger
      mng schedule remove trigger-1 trigger-2 --force
    """
    _mng_ctx, _output_opts, _opts = setup_command_context(
        ctx=ctx,
        command_name="schedule_remove",
        command_class=ScheduleRemoveCliOptions,
    )
    raise NotImplementedError("schedule remove is not implemented yet")
