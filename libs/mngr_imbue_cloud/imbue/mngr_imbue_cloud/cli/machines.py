"""`mngr imbue_cloud machines ...` subcommands (specs/slice-fleet).

A machine is the sized slice VM a remote workspace runs in (its mngr host id
is stable across stop/start). Resizing is record-then-restart: ``resize``
stamps the desired size on the connector and nothing changes until the
machine's next start applies it (in place when its box has room, otherwise
via a restore -- possibly onto a different box, transparently).
"""

from typing import Any

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.primitives import MachineUnits
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MACHINE_UNITS_STEP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MAX_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import is_allowed_machine_units
from imbue.mngr_imbue_cloud.wire_types import WorkspaceInfo


@click.group(name="machines")
def machines() -> None:
    """Inspect and resize remote machines (units of RAM + grow-only disk)."""


def _find_machine_by_ref(workspaces: list[WorkspaceInfo], machine_ref: str) -> WorkspaceInfo | None:
    """Match a machine by mngr host id, connector row id, or friendly host name."""
    for entry in workspaces:
        if machine_ref in (entry.host_id, str(entry.host_db_id), entry.host_name):
            return entry
    return None


def _machine_display_payload(entry: WorkspaceInfo) -> dict[str, Any]:
    """The show/list payload: identity, state, sizes, and the restart-to-apply flag."""
    is_restart_pending = entry.target_memory_units is not None or entry.target_disk_gb is not None
    return {
        "host_db_id": str(entry.host_db_id),
        "host_id": entry.host_id,
        "host_name": entry.host_name,
        "status": entry.status.value.lower(),
        "memory_units": entry.memory_units,
        "target_memory_units": entry.target_memory_units,
        "disk_gb": entry.disk_gb,
        "target_disk_gb": entry.target_disk_gb,
        "is_restart_needed_to_apply": is_restart_pending,
    }


@machines.command(name="show")
@click.argument("machine_ref", required=False, default=None)
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def show_machines(machine_ref: str | None, account: str | None, connector_url: str | None) -> None:
    """Show machine sizes: every machine, or just MACHINE_REF (host id, row id, or name)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    workspaces = client.list_workspaces(token)
    if machine_ref is None:
        emit_json([_machine_display_payload(entry) for entry in workspaces])
        return
    entry = _find_machine_by_ref(workspaces, machine_ref)
    if entry is None:
        fail_with_json(f"No machine matches {machine_ref!r} (tried host id, row id, and name)", error_class="NotFound")
    emit_json(_machine_display_payload(entry))


@machines.command(name="resize")
@click.argument("machine_ref")
@click.option(
    "--units",
    "units",
    type=int,
    default=None,
    help=(
        "Desired size in units (1 unit = 1GiB of machine RAM; vCPUs and bandwidth scale with it). "
        f"Any multiple of {MACHINE_UNITS_STEP} from {MACHINE_UNITS_STEP} to {MAX_MACHINE_UNITS}; up or down."
    ),
)
@click.option(
    "--disk-gb",
    "disk_gb",
    type=int,
    default=None,
    help="Desired data-disk size in GB. Grow-only: a value below the current size is refused.",
)
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def resize_machine(
    machine_ref: str,
    units: int | None,
    disk_gb: int | None,
    account: str | None,
    connector_url: str | None,
) -> None:
    """Record a resize for MACHINE_REF, applied at the machine's next restart.

    Nothing changes until the machine is stopped and started again (the
    desktop client's restart, or `mngr stop` + `mngr start`): the restart
    applies the size in place when the machine's box has room, or restores it
    onto a box that does.
    """
    if units is None and disk_gb is None:
        fail_with_json("Nothing to resize: pass --units and/or --disk-gb", error_class="UsageError")
    validated_units: MachineUnits | None = None
    if units is not None:
        # Client-side validation of the allowed-size set so a typo fails fast
        # with the rule spelled out (the connector re-validates regardless).
        if not is_allowed_machine_units(units):
            fail_with_json(
                f"--units must be a multiple of {MACHINE_UNITS_STEP} between {MACHINE_UNITS_STEP} and "
                f"{MAX_MACHINE_UNITS}, got {units}",
                error_class="UsageError",
            )
        validated_units = MachineUnits(units)
    if disk_gb is not None and disk_gb <= 0:
        fail_with_json(f"--disk-gb must be positive, got {disk_gb}", error_class="UsageError")
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    workspaces = client.list_workspaces(token)
    entry = _find_machine_by_ref(workspaces, machine_ref)
    if entry is None:
        fail_with_json(f"No machine matches {machine_ref!r} (tried host id, row id, and name)", error_class="NotFound")
    recorded = client.resize_machine(
        token, str(entry.host_db_id), target_memory_units=validated_units, target_disk_gb=disk_gb
    )
    is_restart_pending = recorded.get("target_memory_units") is not None or recorded.get("target_disk_gb") is not None
    emit_json(
        {
            "host_db_id": str(entry.host_db_id),
            "host_id": entry.host_id,
            "host_name": entry.host_name,
            "status": recorded.get("status"),
            "memory_units": recorded.get("memory_units"),
            "target_memory_units": recorded.get("target_memory_units"),
            "disk_gb": recorded.get("disk_gb"),
            "target_disk_gb": recorded.get("target_disk_gb"),
            "is_restart_needed_to_apply": is_restart_pending,
            "note": (
                "The new size applies at the machine's next restart (stop it and start it again)."
                if is_restart_pending
                else "The requested size equals the current size; nothing is pending."
            ),
        }
    )
