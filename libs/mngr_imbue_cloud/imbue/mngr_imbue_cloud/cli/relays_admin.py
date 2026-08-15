"""`mngr imbue_cloud admin relays ...` -- operator-only relay fleet management.

The connector's ``relays`` table is the source of truth for the sharing relay
fleet: share creation, the workspace assignment endpoint, frps auth, and the
health-driven DNS reconciliation all read it. The ``share-relay`` provisioning
flow normally registers relays itself; these commands cover inspection and
manual repair. Authenticated by the fixed ``MINDS_ADMIN_KEY`` API key.
"""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli.paid import paid_auth_options
from imbue.mngr_imbue_cloud.cli.paid import resolve_admin_api_key


@click.group(name="relays")
def relays_admin() -> None:
    """Manage the sharing relay fleet (requires the MINDS_ADMIN_KEY API key)."""


@relays_admin.command(name="list")
@paid_auth_options
@handle_imbue_cloud_errors
def relays_list(connector_url: str | None, api_key: str | None) -> None:
    """List every relay row (active and retired) with health state."""
    client = make_connector_client(connector_url)
    rows = client.admin_list_relays(resolve_admin_api_key(api_key))
    emit_json([row.model_dump() for row in rows])


@relays_admin.command(name="add")
@click.option("--relay-id", default=None, help="Existing relay id to update/revive; omit to mint a fresh one")
@click.option("--region", required=True, help="Region code the relay serves (e.g. us1)")
@click.option("--tunnel-endpoint", required=True, help="host:port the workspaces' frpc dials (typically <ip>:7000)")
@click.option("--ip", "ip_address", required=True, help="Public IPv4 (DNS answer + healthz probe target)")
@click.option("--instance-name", default="", help="Human-readable OVH instance name")
@paid_auth_options
@handle_imbue_cloud_errors
def relays_add(
    relay_id: str | None,
    region: str,
    tunnel_endpoint: str,
    ip_address: str,
    instance_name: str,
    connector_url: str | None,
    api_key: str | None,
) -> None:
    """Register (or update/revive) one relay in the fleet inventory."""
    client = make_connector_client(connector_url)
    record = client.admin_register_relay(
        resolve_admin_api_key(api_key),
        relay_id=relay_id,
        region=region,
        tunnel_endpoint=tunnel_endpoint,
        ip_address=ip_address,
        instance_name=instance_name,
    )
    emit_json(record.model_dump())


@relays_admin.command(name="remove")
@click.argument("relay_id")
@paid_auth_options
@handle_imbue_cloud_errors
def relays_remove(relay_id: str, connector_url: str | None, api_key: str | None) -> None:
    """Retire one relay: it leaves assignment, DNS, and frps auth (the row is kept for audit)."""
    client = make_connector_client(connector_url)
    emit_json(client.admin_retire_relay(resolve_admin_api_key(api_key), relay_id))
