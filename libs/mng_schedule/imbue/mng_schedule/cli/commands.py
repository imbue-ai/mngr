# Import command modules to register subcommands on the schedule group.
import imbue.mng_schedule.cli.add as _add  # noqa: F401
import imbue.mng_schedule.cli.list as _list  # noqa: F401
import imbue.mng_schedule.cli.remove as _remove  # noqa: F401
import imbue.mng_schedule.cli.run as _run  # noqa: F401
import imbue.mng_schedule.cli.update as _update  # noqa: F401
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng_schedule.cli.group import schedule

CommandHelpMetadata(
    key="schedule",
    one_line_description="Schedule invocations of mng commands",
    synopsis="mng schedule [add|remove|update|list|run] [OPTIONS]",
    description="""Schedule invocations of mng commands.

Manage cron-scheduled triggers that run mng commands (create, start, message,
exec) on a specified provider at regular intervals. This is useful for setting
up autonomous agents that run on a recurring schedule.""",
    examples=(
        ("Add a nightly scheduled agent", "mng schedule add --command create --schedule '0 2 * * *' --provider modal"),
        ("List all schedules", "mng schedule list --provider local"),
        ("Remove a trigger", "mng schedule remove my-trigger"),
        ("Disable a trigger", "mng schedule update my-trigger --disabled"),
        ("Test a trigger immediately", "mng schedule run my-trigger"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("start", "Start an existing agent"),
        ("exec", "Execute a command on an agent"),
    ),
).register()

add_pager_help_option(schedule)
