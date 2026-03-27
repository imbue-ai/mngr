from typing import Any

import click

from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng_schedule.cli.group import schedule
from imbue.mng_schedule.cli.options import ScheduleRunCliOptions


@schedule.command(name="run")
@click.argument("name", required=True)
@add_common_options
@click.pass_context
def schedule_run(ctx: click.Context, **kwargs: Any) -> None:
    """Run a scheduled trigger immediately.

    Executes the specified trigger's command right now, regardless of its
    cron schedule. Useful for testing triggers before waiting for the
    scheduled time.

    \b
    Examples:
      mng schedule run my-trigger
    """
    _mng_ctx, _output_opts, _opts = setup_command_context(
        ctx=ctx,
        command_name="schedule_run",
        command_class=ScheduleRunCliOptions,
    )
    raise NotImplementedError("schedule run is not implemented yet")
