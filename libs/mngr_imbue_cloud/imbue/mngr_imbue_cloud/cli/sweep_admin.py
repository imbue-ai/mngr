"""`mngr imbue_cloud admin sweep ...` -- operator-only on-demand connector sweeps.

All commands authenticate with the fixed ``MINDS_ADMIN_KEY`` API key,
like the paid-list CRUD and account admin commands.
"""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli.paid import paid_auth_options
from imbue.mngr_imbue_cloud.cli.paid import resolve_admin_api_key


@click.group(name="sweep")
def sweep_admin() -> None:
    """Run connector maintenance sweeps on demand (requires MINDS_ADMIN_KEY)."""


@sweep_admin.command(name="r2")
@click.option("--email", default=None, help="Scope the pass to one account (full pass when omitted)")
@paid_auth_options
@handle_imbue_cloud_errors
def sweep_r2(email: str | None, connector_url: str | None, api_key: str | None) -> None:
    """Run one R2 storage-quota sweep pass (enforcement, grant settlement, key invariants).

    Identical to the hourly cron, but on demand -- useful after bumping a
    quota or to settle a cleanup grant without waiting for the schedule.
    """
    client = make_connector_client(connector_url)
    emit_json(client.admin_run_r2_sweep(resolve_admin_api_key(api_key), email))
