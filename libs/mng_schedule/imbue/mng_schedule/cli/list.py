from typing import Any
from typing import Final
from typing import assert_never

import click
from click_option_group import optgroup
from tabulate import tabulate

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.logging import log_span
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.errors import MngError
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng_modal.instance import ModalProviderInstance
from imbue.mng_schedule.cli.group import schedule
from imbue.mng_schedule.cli.options import ScheduleListCliOptions
from imbue.mng_schedule.data_types import ScheduleCreationRecord
from imbue.mng_schedule.implementations.local.deploy import list_local_schedule_creation_records
from imbue.mng_schedule.implementations.modal.deploy import list_schedule_creation_records


@schedule.command(name="list")
@optgroup.group("Filtering")
@optgroup.option(
    "-a",
    "--all",
    "all_schedules",
    is_flag=True,
    help="Show all schedules, including disabled ones.",
)
@optgroup.option(
    "--provider",
    required=True,
    help="Provider instance to list schedules from (e.g. 'local', 'modal').",
)
@add_common_options
@click.pass_context
def schedule_list(ctx: click.Context, **kwargs: Any) -> None:
    """List scheduled triggers.

    Shows all active scheduled triggers. Use --all to include disabled triggers.

    \b
    Examples:
      mng schedule list --provider local
      mng schedule list --provider modal --all
      mng schedule list --provider local --format=json
    """
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="schedule_list",
        command_class=ScheduleListCliOptions,
    )

    # Load the provider instance
    try:
        provider = get_provider_instance(ProviderInstanceName(opts.provider), mng_ctx)
    except MngError as e:
        raise click.ClickException(f"Failed to load provider '{opts.provider}': {e}") from e

    if isinstance(provider, LocalProviderInstance):
        with log_span("Listing local schedule creation records"):
            records: list[ScheduleCreationRecord] = list_local_schedule_creation_records(mng_ctx)
    elif isinstance(provider, ModalProviderInstance):
        with log_span("Listing schedule creation records"):
            records = list(list_schedule_creation_records(provider))
    else:
        raise click.ClickException(
            f"Provider '{opts.provider}' (type {type(provider).__name__}) is not supported for schedules. "
            "Supported providers: local, modal."
        )

    # Filter out disabled schedules unless --all is specified
    if not opts.all_schedules:
        records = [r for r in records if r.trigger.is_enabled]

    # Sort by creation time (oldest first)
    records_sorted = sorted(records, key=lambda r: r.created_at)

    match output_opts.output_format:
        case OutputFormat.JSON:
            _emit_schedule_list_json(records_sorted)
        case OutputFormat.JSONL:
            _emit_schedule_list_jsonl(records_sorted)
        case OutputFormat.HUMAN:
            _emit_schedule_list_human(records_sorted)
        case _ as unreachable:
            assert_never(unreachable)


# =============================================================================
# Output helpers for schedule list
# =============================================================================


_SCHEDULE_LIST_DISPLAY_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "command",
    "schedule",
    "enabled",
    "provider",
    "git_hash",
    "created_at",
    "hostname",
)

_SCHEDULE_LIST_HEADERS: Final[dict[str, str]] = {
    "name": "NAME",
    "command": "COMMAND",
    "schedule": "SCHEDULE",
    "enabled": "ENABLED",
    "provider": "PROVIDER",
    "git_hash": "GIT HASH",
    "created_at": "CREATED",
    "hostname": "HOST",
}


def _get_schedule_field_value(record: ScheduleCreationRecord, field: str) -> str:
    """Extract a display value from a ScheduleCreationRecord."""
    match field:
        case "name":
            return record.trigger.name
        case "command":
            return record.trigger.command.value.lower()
        case "schedule":
            return record.trigger.schedule_cron
        case "enabled":
            return "yes" if record.trigger.is_enabled else "no"
        case "provider":
            return record.trigger.provider
        case "git_hash":
            git_hash = record.trigger.git_image_hash
            return git_hash[:12] if git_hash else ""
        case "created_at":
            return record.created_at.strftime("%Y-%m-%d %H:%M")
        case "hostname":
            return record.hostname
        case _:
            raise SwitchError(f"Unknown schedule display field: {field}")


def _emit_schedule_list_human(records: list[ScheduleCreationRecord]) -> None:
    """Emit human-readable table output for schedule list."""
    if not records:
        write_human_line("No schedules found")
        return

    headers = [_SCHEDULE_LIST_HEADERS[f] for f in _SCHEDULE_LIST_DISPLAY_FIELDS]
    rows: list[list[str]] = []
    for record in records:
        row = [_get_schedule_field_value(record, f) for f in _SCHEDULE_LIST_DISPLAY_FIELDS]
        rows.append(row)

    table = tabulate(rows, headers=headers, tablefmt="plain")
    write_human_line("\n" + table)


def _emit_schedule_list_json(records: list[ScheduleCreationRecord]) -> None:
    """Emit JSON output for schedule list."""
    data = {
        "schedules": [record.model_dump(mode="json") for record in records],
    }
    emit_final_json(data)


def _emit_schedule_list_jsonl(records: list[ScheduleCreationRecord]) -> None:
    """Emit JSONL output for schedule list."""
    for record in records:
        emit_final_json(record.model_dump(mode="json"))
