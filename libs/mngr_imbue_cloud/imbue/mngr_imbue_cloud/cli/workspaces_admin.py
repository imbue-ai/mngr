"""`mngr imbue_cloud admin workspaces ...` -- operator workspace-lifecycle escape hatches.

Authenticated with the fixed ``MINDS_ADMIN_KEY`` API key, like the paid-list
CRUD. ``abandon`` marks a workspace ``crashed`` -- the lever for a row whose
box is permanently dead and whose stop/start transition would otherwise
retry forever. The user recovers by restoring the workspace's backup into a
fresh workspace; artifacts and any surviving VM are reclaimed at release.
"""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli.paid import paid_auth_options
from imbue.mngr_imbue_cloud.cli.paid import resolve_admin_api_key


@click.group(name="workspaces")
def workspaces_admin() -> None:
    """Operator workspace-lifecycle management (requires MINDS_ADMIN_KEY)."""


@workspaces_admin.command(name="abandon")
@click.argument("host_db_id")
@click.option("--reason", required=True, help="Why the workspace is being abandoned (recorded on the row)")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_abandon_workspace(host_db_id: str, reason: str, connector_url: str | None, api_key: str | None) -> None:
    """Mark the workspace HOST_DB_ID crashed (its box is permanently dead)."""
    client = make_connector_client(connector_url)
    client.admin_abandon_workspace(resolve_admin_api_key(api_key), host_db_id, reason)
    emit_json({"host_db_id": host_db_id, "status": "crashed", "reason": reason})
