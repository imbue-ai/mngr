from typing import Any

import click

from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng_schedule.cli.group import add_trigger_options
from imbue.mng_schedule.cli.group import resolve_positional_name
from imbue.mng_schedule.cli.group import schedule
from imbue.mng_schedule.cli.options import ScheduleAddCliOptions


@schedule.command(name="update")
@add_trigger_options
@add_common_options
@click.pass_context
def schedule_update(ctx: click.Context, **kwargs: Any) -> None:
    """Update an existing scheduled trigger.

    Alias for 'add --update'. Accepts the same options as the add command.
    """
    resolve_positional_name(ctx)
    ctx.params["update"] = True
    # Set defaults for add-specific fields that are not on the update command
    ctx.params.setdefault("auto_fix_args", True)
    ctx.params.setdefault("ensure_safe_commands", True)
    _mng_ctx, _output_opts, _opts = setup_command_context(
        ctx=ctx,
        command_name="schedule_update",
        command_class=ScheduleAddCliOptions,
    )
    raise NotImplementedError("schedule update is not implemented yet")
