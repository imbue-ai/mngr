"""`mngr imbue_cloud hosts ...` subcommands.

Lease creation goes through ``mngr create --provider imbue_cloud_<account>
--new-host -b <attr>=<val> ...``; the provider implementation issues the
lease, runs the SSH bootstrap, and returns a host that the standard mngr
create pipeline finishes adopting under the caller's chosen agent name.
These subcommands are listing + release helpers on top of that flow.
"""

import click

from imbue.mngr_imbue_cloud.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active


@click.group(name="hosts")
def hosts() -> None:
    """List and release leased pool hosts."""


@hosts.command(name="list")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_hosts(account: str | None, connector_url: str | None) -> None:
    """List all hosts currently leased by this account."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    leased = client.list_hosts(token)
    payload = [
        {
            "host_db_id": str(entry.host_db_id),
            "host_id": entry.host_id,
            "agent_id": entry.agent_id,
            "vps_ip": entry.vps_ip,
            "ssh_user": entry.ssh_user,
            "ssh_port": entry.ssh_port,
            "container_ssh_port": entry.container_ssh_port,
            "attributes": entry.attributes,
            "leased_at": entry.leased_at,
        }
        for entry in leased
    ]
    emit_json(payload)


@hosts.command(name="release")
@click.argument("host_db_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def release_host(host_db_id: str, account: str | None, connector_url: str | None) -> None:
    """Release a leased host back to the pool."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    is_released = client.release_host(token, host_db_id)
    if not is_released:
        fail_with_json("Connector returned non-success on release", error_class="ReleaseFailed")
    emit_json({"released": True, "host_db_id": host_db_id})
