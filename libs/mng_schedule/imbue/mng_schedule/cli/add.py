import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.deploy_utils import MngInstallMode
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.providers.modal.instance import ModalProviderInstance
from imbue.mng_schedule.cli.group import add_trigger_options
from imbue.mng_schedule.cli.group import resolve_positional_name
from imbue.mng_schedule.cli.group import schedule
from imbue.mng_schedule.cli.options import ScheduleAddCliOptions
from imbue.mng_schedule.data_types import ScheduleTriggerDefinition
from imbue.mng_schedule.data_types import ScheduledMngCommand
from imbue.mng_schedule.data_types import VerifyMode
from imbue.mng_schedule.errors import ScheduleDeployError
from imbue.mng_schedule.git import resolve_current_branch_name
from imbue.mng_schedule.implementations.local.deploy import deploy_local_schedule
from imbue.mng_schedule.implementations.modal.deploy import deploy_schedule
from imbue.mng_schedule.implementations.modal.deploy import parse_upload_spec

# =============================================================================
# Auto-fix and safety check logic
# =============================================================================


@pure
def _split_args_at_separator(parts: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split a list of args at the first '--' separator.

    Returns (mng_args, passthrough_args) where passthrough_args includes
    the '--' separator itself.
    """
    parts_list = list(parts)
    try:
        separator_idx = parts_list.index("--")
        return parts_list[:separator_idx], parts_list[separator_idx:]
    except ValueError:
        return parts_list, []


@pure
def _arg_matches(arg: str, flag: str) -> bool:
    """Check if an arg matches a flag, handling both '--flag' and '--flag=value' forms."""
    return arg == flag or arg.startswith(f"{flag}=")


@pure
def _has_flag(mng_args: Sequence[str], flag: str, negative_flag: str | None = None) -> bool:
    """Check if a flag (or its negative counterpart) is present in the args.

    Handles both '--flag' and '--flag=value' forms.
    """
    for arg in mng_args:
        if _arg_matches(arg, flag):
            return True
        if negative_flag is not None and _arg_matches(arg, negative_flag):
            return True
    return False


@pure
def _has_tag_with_key(mng_args: Sequence[str], tag_key: str) -> bool:
    """Check if a --tag with the given key prefix exists in the args.

    Handles both '--tag KEY=VALUE' (two tokens) and '--tag=KEY=VALUE' (single token) forms.
    """
    for i, part in enumerate(mng_args):
        # Two-token form: --tag KEY=VALUE
        if part == "--tag" and i + 1 < len(mng_args) and mng_args[i + 1].startswith(f"{tag_key}="):
            return True
        # Single-token form: --tag=KEY=VALUE
        if part.startswith(f"--tag={tag_key}="):
            return True
    return False


# FIXME: there really should be a "--headless" and "--interactive" flags in mng itself (as common options that apply across every command and raise an Exception if eg you try to be interactive but are not in a TTY). That way, if --headless is set, we can NEVER do interactive things (we have some logic for understnding if we're in interactive mode, it should be made consistent and use that common flag).
#  When implementing this, once it's done, also come back here and add "--headless" as a default flag for *any* command (not just create)
@pure
def auto_fix_create_args(
    args: str,
    trigger_name: str,
    ssh_public_key: str | None,
) -> str:
    """Auto-fix args for a create command to ensure they work as expected.

    Adds the following flags if not already present:
    - --no-connect: so we don't try to automatically connect
    - --await-ready: to make sure the command actually worked
    - --authorized-key <key>: so you can connect to the host via SSH
    - --tag SCHEDULE=<name>: to make it easy to filter scheduled agents

    Only the mng args (before any '--' separator) are checked and modified.
    """
    parts = shlex.split(args) if args else []
    mng_args, passthrough_args = _split_args_at_separator(parts)

    # FIXME: we should check that "--yes" is specified, and add it if not

    if not _has_flag(mng_args, "--no-connect", "--connect"):
        mng_args.append("--no-connect")

    if not _has_flag(mng_args, "--await-ready", "--no-await-ready"):
        mng_args.append("--await-ready")

    if ssh_public_key is not None and not _has_flag(mng_args, "--authorized-key"):
        mng_args.extend(["--authorized-key", ssh_public_key])

    if not _has_tag_with_key(mng_args, "SCHEDULE"):
        mng_args.extend(["--tag", f"SCHEDULE={trigger_name}"])

    return shlex.join(mng_args + passthrough_args)


@pure
def check_safe_create_command(args: str) -> str | None:
    """Check that create command args are safe for scheduled execution.

    Returns None if args are safe, or an error message string if not.

    Currently checks:
    - Either --new-branch with a {DATE} placeholder in its value, or --reuse
      must be specified, so that each scheduled run doesn't conflict.
    """
    parts = shlex.split(args) if args else []
    mng_args, _passthrough_args = _split_args_at_separator(parts)

    if _has_flag(mng_args, "--reuse"):
        return None

    # Check for --new-branch with a {DATE} placeholder in its value.
    # Handles three forms:
    # 1. "--new-branch value" (two tokens, space-separated)
    # 2. "--new-branch=value" (single token, equals-separated)
    # 3. "--new-branch" alone (flag mode, no value -- not sufficient)
    for i, part in enumerate(mng_args):
        # Single-token form: --new-branch=value
        if part.startswith("--new-branch="):
            branch_value = part[len("--new-branch=") :]
            if "{DATE}" in branch_value:
                return None
        # Two-token form: --new-branch value
        elif part == "--new-branch" and i + 1 < len(mng_args):
            next_arg = mng_args[i + 1]
            # If the next arg looks like another flag, --new-branch was used
            # as a flag (no value), so skip it.
            if not next_arg.startswith("-") and "{DATE}" in next_arg:
                return None

    return (
        "Create command should either use --new-branch with a {DATE} placeholder "
        "(e.g. --new-branch 'my-branch-{DATE}') or --reuse to avoid creating "
        "conflicting agents/branches on each scheduled run."
    )


def _get_provider_ssh_public_key(
    provider: LocalProviderInstance | ModalProviderInstance,
) -> str | None:
    """Get the SSH public key for the given provider, or None if not applicable.

    For modal: returns the provider's SSH public key (for agent --authorized-key).
    For local: returns None (local provider doesn't use SSH for agent connections).
    """
    if isinstance(provider, ModalProviderInstance):
        return provider.get_ssh_public_key()
    elif isinstance(provider, LocalProviderInstance):
        return None
    else:
        raise TypeError(f"Unsupported provider type: {type(provider).__name__}")


# =============================================================================
# CLI command
# =============================================================================


@schedule.command(name="add")
@add_trigger_options
@optgroup.group("Add-specific")
@optgroup.option(
    "--update",
    is_flag=True,
    help="If a schedule with the same name already exists, update it instead of failing.",
)
@optgroup.option(
    "--auto-fix-args/--no-auto-fix-args",
    "auto_fix_args",
    default=True,
    show_default=True,
    help="Automatically add args to create commands to make sure they work as expected "
    "(e.g. --no-connect, --await-ready, --authorized-key, --tag SCHEDULE=<name>).",
)
@optgroup.option(
    "--ensure-safe-commands/--no-ensure-safe-commands",
    "ensure_safe_commands",
    default=True,
    show_default=True,
    help="Error if the scheduled command looks unsafe (e.g. missing --new-branch {DATE} or --reuse). "
    "Pass --no-ensure-safe-commands to downgrade these errors to warnings.",
)
@add_common_options
@click.pass_context
def schedule_add(ctx: click.Context, **kwargs: Any) -> None:
    """Add a new scheduled trigger.

    Creates a new cron-scheduled trigger that will run the specified mng
    command at the specified interval on the specified provider.

    For local provider: uses the system crontab to schedule the command.
    For modal provider: packages code and deploys a Modal cron function.

    Note that you are responsible for ensuring the correct env vars and files are passed through (this command
    automatically includes user and project settings for mng and any enabled plugins, but you may need to include
    additional env vars or files for your specific remote mng command to run correctly). See the options below for
    how to include env files and uploads in the deployment.

    \b
    Examples:
      mng schedule add --command create --args "--type claude --message 'fix bugs' --in local" --schedule "0 2 * * *" --provider local
      mng schedule add --command create --args "--type claude --message 'fix bugs' --in modal" --schedule "0 2 * * *" --provider modal
    """
    resolve_positional_name(ctx)
    # New schedules default to enabled. The shared options use None so that
    # update can distinguish "not specified" from "explicitly set".
    if ctx.params.get("enabled") is None:
        ctx.params["enabled"] = True
    mng_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="schedule_add",
        command_class=ScheduleAddCliOptions,
    )

    # Default --command to "create" when not specified
    effective_command = opts.command if opts.command is not None else "create"

    # Validate required options for add
    if opts.schedule_cron is None:
        raise click.UsageError("--schedule is required for schedule add")
    if opts.provider is None:
        raise click.UsageError("--provider is required for schedule add")

    # Code packaging strategy validation
    if opts.snapshot_id is not None:
        raise NotImplementedError("--snapshot is not yet implemented for schedule add")

    # Load the provider instance
    try:
        provider = get_provider_instance(ProviderInstanceName(opts.provider), mng_ctx)
    except MngError as e:
        raise click.ClickException(f"Failed to load provider '{opts.provider}': {e}") from e

    if not isinstance(provider, (LocalProviderInstance, ModalProviderInstance)):
        raise click.ClickException(
            f"Provider '{opts.provider}' (type {type(provider).__name__}) is not supported for schedules. "
            "Supported providers: local, modal."
        )

    # Generate name if not provided
    trigger_name = opts.name if opts.name else f"trigger-{uuid4().hex[:8]}"

    command = ScheduledMngCommand(effective_command.upper())
    raw_args = opts.args or ""
    final_args = raw_args

    # Apply auto-fix and safety checks for create commands
    if command == ScheduledMngCommand.CREATE:
        if opts.auto_fix_args:
            ssh_public_key = _get_provider_ssh_public_key(provider)
            final_args = auto_fix_create_args(raw_args, trigger_name, ssh_public_key)
            logger.info("Auto-fixed args for create command: {}", final_args)

        safety_issue = check_safe_create_command(final_args)
        if safety_issue is not None:
            if opts.ensure_safe_commands:
                raise click.UsageError(safety_issue)
            else:
                logger.warning(safety_issue)

    trigger = ScheduleTriggerDefinition(
        name=trigger_name,
        command=command,
        args=final_args,
        schedule_cron=opts.schedule_cron,
        provider=opts.provider,
        is_enabled=opts.enabled if opts.enabled is not None else True,
    )

    if isinstance(provider, LocalProviderInstance):
        if opts.full_copy:
            logger.warning("--full-copy has no effect for the local provider (code is run from the current directory)")
        _deploy_local(trigger, mng_ctx, opts)
    elif isinstance(provider, ModalProviderInstance):
        _deploy_modal(trigger, mng_ctx, opts, provider)


def _deploy_local(
    trigger: ScheduleTriggerDefinition,
    mng_ctx: MngContext,
    opts: ScheduleAddCliOptions,
) -> None:
    """Deploy a schedule to the local provider using crontab."""
    try:
        deploy_local_schedule(
            trigger,
            mng_ctx,
            sys_argv=sys.argv,
            pass_env=opts.pass_env,
            env_files=tuple(Path(f) for f in opts.env_files),
        )
    except ScheduleDeployError as e:
        raise click.ClickException(str(e)) from e

    logger.info("Schedule '{}' deployed to local crontab", trigger.name)
    click.echo(f"Deployed schedule '{trigger.name}' to local crontab")


def _deploy_modal(
    trigger: ScheduleTriggerDefinition,
    mng_ctx: MngContext,
    opts: ScheduleAddCliOptions,
    provider: ModalProviderInstance,
) -> None:
    """Deploy a schedule to a Modal provider."""
    # Resolve verification mode from CLI option.
    # Only apply verification for create commands (other commands don't produce agents).
    verify_mode = VerifyMode(opts.verify.upper())
    if verify_mode != VerifyMode.NONE and trigger.command != ScheduledMngCommand.CREATE:
        logger.debug(
            "Skipping verification for command '{}': only applicable to 'create' commands",
            trigger.command,
        )
        verify_mode = VerifyMode.NONE

    # Resolve deploy file options (default to True for add)
    include_user_settings = opts.include_user_settings if opts.include_user_settings is not None else True
    include_project_settings = opts.include_project_settings if opts.include_project_settings is not None else True

    # Parse upload specs
    parsed_uploads: list[tuple[Path, str]] = []
    for upload_spec in opts.uploads:
        try:
            parsed_uploads.append(parse_upload_spec(upload_spec))
        except ValueError as e:
            raise click.UsageError(str(e)) from e

    # Resolve auto-merge branch: default to current branch if --auto-merge is on
    auto_merge_branch: str | None = None
    if opts.auto_merge:
        if opts.auto_merge_branch is not None:
            auto_merge_branch = opts.auto_merge_branch
        else:
            try:
                auto_merge_branch = resolve_current_branch_name()
            except ScheduleDeployError as e:
                raise click.ClickException(
                    f"--auto-merge requires a git branch, but could not resolve one: {e}. "
                    "Use --no-auto-merge or --auto-merge-branch to specify explicitly."
                ) from e
        logger.info("Auto-merge enabled for branch '{}'", auto_merge_branch)

    try:
        app_name = deploy_schedule(
            trigger,
            mng_ctx,
            provider=provider,
            verify_mode=verify_mode,
            sys_argv=sys.argv,
            include_user_settings=include_user_settings,
            include_project_settings=include_project_settings,
            pass_env=opts.pass_env,
            env_files=tuple(Path(f) for f in opts.env_files),
            uploads=parsed_uploads,
            mng_install_mode=MngInstallMode(opts.mng_install_mode.upper()),
            target_repo_path=opts.target_dir,
            auto_merge_branch=auto_merge_branch,
            is_full_copy=opts.full_copy,
        )
    except ScheduleDeployError as e:
        raise click.ClickException(str(e)) from e

    logger.info("Schedule '{}' deployed as Modal app '{}'", trigger.name, app_name)
    click.echo(f"Deployed schedule '{trigger.name}' as Modal app '{app_name}'")
