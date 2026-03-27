from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.group import file_group
from imbue.mngr_file.cli.list import file_list
from imbue.mngr_file.cli.put import file_put

# Ensure subcommand registrations are visible to this module's importers
_SUBCOMMANDS = (file_get, file_put, file_list)

CommandHelpMetadata(
    key="file",
    one_line_description="Read, write, and list files on agents and hosts",
    synopsis="mngr file (get|put|list) TARGET PATH [OPTIONS]",
    description="""Transfer files to and from agents and hosts.

Use 'get' to read a file, 'put' to write a file, and 'list' to list
files in a directory. TARGET can be an agent or host name/ID.

Paths can be absolute or relative. For agent targets, relative paths
are resolved against the agent's work directory by default. Use
--relative-to to change the base: 'state' for the agent state
directory, or 'host' for the host directory. For host targets,
relative paths always resolve against the host directory.""",
    examples=(
        ("Read a file from an agent", "mngr file get my-agent config.toml"),
        ("Write a file to an agent", "mngr file put my-agent config.toml --input local.toml"),
        ("List files in an agent's work directory", "mngr file list my-agent"),
        ("List files relative to agent state directory", "mngr file list my-agent --relative-to state"),
        ("Read a file using absolute path", "mngr file get my-agent /etc/hostname"),
        ("Write stdin to a file on a host", "echo 'hello' | mngr file put my-host greeting.txt"),
    ),
    see_also=(
        ("exec", "Execute a shell command on an agent's host"),
        ("push", "Push files or git commits to an agent"),
        ("pull", "Pull files or git commits from an agent"),
        ("events", "View agent and host event files"),
    ),
).register()

add_pager_help_option(file_group)
