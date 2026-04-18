from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_schedule.cli.group import schedule
from imbue.mngr_schedule.cli.options import ScheduleRemoveCliOptions
from imbue.mngr_schedule.cli.provider_utils import load_schedule_provider
from imbue.mngr_schedule.implementations.local.deploy import get_local_schedule_creation_record
from imbue.mngr_schedule.implementations.local.deploy import remove_local_schedule
from imbue.mngr_schedule.implementations.modal.deploy import get_modal_schedule_creation_record
from imbue.mngr_schedule.implementations.modal.deploy import remove_modal_schedule


@schedule.command(name="remove")
@click.argument("names", nargs=-1, required=True)
@optgroup.group("Execution")
@optgroup.option(
    "--provider",
    required=True,
    help="Provider on which the triggers are deployed (e.g. 'local', 'modal').",
)
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

    Removes the deployed trigger and all associated artifacts (crontab
    entries, wrapper scripts, creation records, Modal apps).

    Removal is idempotent: if a trigger is partially removed, running
    remove again will clean up whatever remains.

    \b
    Examples:
      mngr schedule remove my-trigger --provider local
      mngr schedule remove trigger-1 trigger-2 --provider modal --force
    """
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="schedule_remove",
        command_class=ScheduleRemoveCliOptions,
    )

    provider = load_schedule_provider(opts.provider, mngr_ctx)

    # Verify triggers exist (warn for missing ones but don't abort)
    found_names, missing_names = _check_triggers_exist(provider, opts.names, mngr_ctx)

    if missing_names:
        for name in missing_names:
            logger.warning("No schedule record found for trigger '{}' on provider '{}'", name, opts.provider)

    if not found_names and not missing_names:
        write_human_line("No triggers specified.")
        return

    # Confirm with user unless --force
    if not opts.force and found_names:
        write_human_line("\nThe following triggers will be removed:")
        for name in found_names:
            write_human_line("  - {}", name)
        if missing_names:
            write_human_line("\nThe following triggers were not found (skipped):")
            for name in missing_names:
                write_human_line("  - {}", name)
        write_human_line("")
        if not click.confirm("Are you sure you want to continue?"):
            raise SystemExit(0)

    # Remove each trigger
    for name in found_names:
        if isinstance(provider, LocalProviderInstance):
            remove_local_schedule(name, mngr_ctx)
        elif isinstance(provider, ModalProviderInstance):
            remove_modal_schedule(provider, name)
        else:
            assert_never(provider)
        write_human_line("Removed schedule '{}'", name)


def _check_triggers_exist(
    provider: LocalProviderInstance | ModalProviderInstance,
    names: tuple[str, ...],
    mngr_ctx: MngrContext,
) -> tuple[list[str], list[str]]:
    """Check which triggers exist on the provider.

    Returns (found_names, missing_names).
    """
    found: list[str] = []
    missing: list[str] = []
    for name in names:
        if isinstance(provider, LocalProviderInstance):
            record = get_local_schedule_creation_record(mngr_ctx, name)
        elif isinstance(provider, ModalProviderInstance):
            record = get_modal_schedule_creation_record(provider, name)
        else:
            assert_never(provider)

        if record is not None:
            found.append(name)
        else:
            missing.append(name)
    return found, missing
