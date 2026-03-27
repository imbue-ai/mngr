# Import command modules to register subcommands on the schedule group.
import imbue.mngr_schedule.cli.add as _add  # noqa: F401
import imbue.mngr_schedule.cli.list as _list  # noqa: F401
import imbue.mngr_schedule.cli.remove as _remove  # noqa: F401
import imbue.mngr_schedule.cli.run as _run  # noqa: F401
import imbue.mngr_schedule.cli.update as _update  # noqa: F401
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr_schedule.cli.group import schedule

CommandHelpMetadata(
    key="schedule",
    one_line_description="Schedule invocations of mngr commands",
    synopsis="mngr schedule [add|remove|update|list|run] [OPTIONS]",
    description="""Manage cron-scheduled triggers that run mngr commands (create, start, message,
exec) on a specified provider at regular intervals. This is useful for setting
up autonomous agents that run on a recurring schedule.""",
    examples=(
        (
            "Add a nightly scheduled agent",
            "mngr schedule add --command create --schedule '0 2 * * *' --provider modal",
        ),
        ("List all schedules", "mngr schedule list --provider local"),
        ("Remove a trigger", "mngr schedule remove my-trigger"),
        ("Disable a trigger", "mngr schedule update my-trigger --disabled"),
        ("Test a trigger immediately", "mngr schedule run my-trigger"),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("start", "Start an existing agent"),
        ("exec", "Execute a command on an agent"),
    ),
).register()

add_pager_help_option(schedule)
