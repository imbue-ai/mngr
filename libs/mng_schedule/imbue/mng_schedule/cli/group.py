from typing import Any

import click
from click_option_group import optgroup

from imbue.mng.cli.default_command_group import DefaultCommandGroup

# =============================================================================
# Shared option decorator
# =============================================================================


def add_trigger_options(command: Any) -> Any:
    """Add trigger definition options shared by add and update commands.

    All options are optional at the click level. Commands that require specific
    options (e.g. add requires --schedule, --provider) should validate at
    runtime.
    """
    # Applied in reverse order (bottom-up per click convention)

    # Optional positional argument for the name (alternative to --name)
    command = click.argument("positional_name", default=None, required=False)(command)

    # Behavior group
    command = optgroup.option(
        "--auto-merge-branch",
        "auto_merge_branch",
        default=None,
        help="Branch to fetch and merge at runtime before running the command. "
        "Defaults to the current branch when --auto-merge is enabled.",
    )(command)
    command = optgroup.option(
        "--auto-merge/--no-auto-merge",
        "auto_merge",
        default=True,
        show_default=True,
        help="Fetch and merge the latest code from the target branch before each scheduled run. "
        "Requires GH_TOKEN in the environment (via --pass-env or --env-file).",
    )(command)
    command = optgroup.option(
        "--verify",
        type=click.Choice(["none", "quick", "full"], case_sensitive=False),
        default="quick",
        show_default=True,
        help="Post-deploy verification: 'none' skips, 'quick' invokes and destroys agent, 'full' lets agent run to completion.",
    )(command)
    command = optgroup.option(
        "--enabled/--disabled",
        "enabled",
        default=None,
        help="Whether the schedule is enabled.",
    )(command)
    command = optgroup.group("Behavior")(command)

    # Execution group
    command = optgroup.option(
        "--provider",
        default=None,
        help="Provider in which to schedule the call (e.g. 'local', 'modal').",
    )(command)
    command = optgroup.group("Execution")(command)

    # Deploy Files group
    command = optgroup.option(
        "--upload",
        "uploads",
        multiple=True,
        help="Upload a file or directory into the deployed function (SOURCE:DEST format, repeatable). "
        "DEST paths starting with '~' go to the home directory; relative paths go to the project directory.",
    )(command)
    command = optgroup.option(
        "--env-file",
        "env_files",
        multiple=True,
        type=click.Path(exists=True),
        help="Include an env file in the deployed function (repeatable). "
        "Variables are available to both the scheduled runner and the mng command.",
    )(command)
    command = optgroup.option(
        "--pass-env",
        "pass_env",
        multiple=True,
        help="Forward an environment variable from the current shell into the deployed function (repeatable).",
    )(command)
    command = optgroup.option(
        "--include-project-settings/--exclude-project-settings",
        "include_project_settings",
        default=None,
        help="Include or exclude unversioned project-specific settings files. Default: include.",
    )(command)
    command = optgroup.option(
        "--include-user-settings/--exclude-user-settings",
        "include_user_settings",
        default=None,
        help="Include or exclude user home directory settings files (e.g. ~/.mng/, ~/.claude.json). Default: include.",
    )(command)
    command = optgroup.group("Deploy Files")(command)

    # Code Packaging group
    command = optgroup.option(
        "--target-dir",
        "target_dir",
        default="/code/project",
        show_default=True,
        help="Directory inside the container where the target repo will be extracted.",
    )(command)
    command = optgroup.option(
        "--full-copy",
        "full_copy",
        is_flag=True,
        default=False,
        help="Copy the entire codebase into the deployed function's storage. Simple but slow for large codebases.",
    )(command)
    command = optgroup.option(
        "--snapshot",
        "snapshot_id",
        default=None,
        help="Use an existing snapshot for code packaging instead of the git repo.",
    )(command)
    command = optgroup.option(
        "--mng-install-mode",
        "mng_install_mode",
        type=click.Choice(["auto", "package", "editable", "skip"], case_sensitive=False),
        default="auto",
        show_default=True,
        help="How to make mng available in the deployed image: "
        "'auto' detects based on current install, "
        "'package' installs from PyPI, "
        "'editable' packages local source, "
        "'skip' assumes mng is already in the base image.",
    )(command)
    command = optgroup.group("Code Packaging")(command)

    # Trigger Definition group
    command = optgroup.option(
        "--schedule",
        "schedule_cron",
        default=None,
        help="Cron schedule expression defining when the command runs (e.g. '0 2 * * *').",
    )(command)
    command = optgroup.option(
        "--args",
        "args",
        default=None,
        help="Arguments to pass to the mng command (as a string).",
    )(command)
    command = optgroup.option(
        "--command",
        "command",
        type=click.Choice(["create", "start", "message", "exec"], case_sensitive=False),
        default=None,
        help="Which mng command to run when triggered.",
    )(command)
    command = optgroup.option(
        "--name",
        default=None,
        help="Name for this scheduled trigger. If not specified, a random name is generated.",
    )(command)
    command = optgroup.group("Trigger Definition")(command)

    return command


def resolve_positional_name(ctx: click.Context) -> None:
    """Merge the optional positional NAME into the --name option.

    If only the positional is provided, it becomes the --name value.
    If both are provided, raise a UsageError.
    """
    positional = ctx.params.get("positional_name")
    option = ctx.params.get("name")
    if positional and option:
        raise click.UsageError("Cannot specify both a positional NAME and --name.")
    if positional:
        ctx.params["name"] = positional


# =============================================================================
# CLI Group
# =============================================================================


class _ScheduleGroup(DefaultCommandGroup):
    """Schedule group that defaults to 'add' when no subcommand is given."""

    _default_command: str = "add"


@click.group(name="schedule", cls=_ScheduleGroup)
@click.pass_context
def schedule(ctx: click.Context, **kwargs: Any) -> None:
    """Schedule invocations of mng commands.

    Manage cron-scheduled triggers that run mng commands (create, start,
    message, exec) on a specified provider at regular intervals.

    \b
    Examples:
      mng schedule add --command create --args '--message "do work" --in local' --schedule "0 2 * * *" --provider local
      mng schedule list --provider local
      mng schedule remove my-trigger
      mng schedule run my-trigger
    """
