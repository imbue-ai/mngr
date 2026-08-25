"""`mngr imbue_cloud shares ...` subcommands (self-hosted relay sharing)."""

import click

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.primitives import WorkspaceId
from imbue.mngr_imbue_cloud.wire_types import ShareInfo


@click.group(name="shares")
def shares() -> None:
    """Manage self-hosted workspace shares (relay tunnels + workspace-terminated TLS)."""


def _share_to_json(info: ShareInfo, include_token: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "host_id": info.host_id,
        "workspace_domain": info.workspace_domain,
        "region": info.region,
        "state": info.state,
        "relay_endpoints": [entry.model_dump() for entry in info.relay_endpoints],
        # Per-relay tunnel login stamps (populated by status documents only).
        "relays": [entry.model_dump() for entry in info.relays],
        "last_tunnel_login_at": info.last_tunnel_login_at,
        "cert_not_after": info.cert_not_after,
    }
    if include_token:
        payload["relay_token"] = info.relay_token.get_secret_value() if info.relay_token else None
    return payload


@shares.command(name="create")
@click.argument("host_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@click.option(
    "--entry-label",
    default=None,
    help=(
        "The workspace's shell-service origin label (e.g. system_interface-<rand>); the hosted "
        "web chrome enters the workspace at <entry-label>.<workspace-domain>. Omit to keep any "
        "previously recorded label."
    ),
)
@click.option(
    "--preferred-region",
    default=None,
    help=(
        "Preferred relay region code (e.g. us1) for a first-time share of a local workspace. "
        "Ignored for pool hosts, unknown regions, and re-shares (the existing region sticks)."
    ),
)
@click.option(
    "--workspace-id",
    default=None,
    help=(
        "The workspace's id (agent-<hex>). When given, the share is workspace-keyed: its domain "
        "leads with a minted, persisted share label instead of the host id, and re-shares resolve "
        "through the workspace id."
    ),
)
@handle_imbue_cloud_errors
def create_share(
    host_id: str,
    account: str | None,
    connector_url: str | None,
    entry_label: str | None,
    preferred_region: str | None,
    workspace_id: str | None,
) -> None:
    """Enable sharing for the given workspace (prints the one-time relay token).

    HOST_ID is the machine the workspace currently runs on; pass
    --workspace-id to key the share by the workspace itself.
    """
    if workspace_id is not None:
        # Reject malformed (or machine-shaped) ids here rather than letting
        # them ride to the connector: a workspace id is always the workspace's
        # agent-<32hex> services-agent id.
        try:
            WorkspaceId(workspace_id)
        except InvalidRandomIdError as exc:
            fail_with_json(f"invalid workspace id: {exc}", error_class="UsageError", exit_code=2)
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    info = client.create_share(
        token, host_id, entry_label=entry_label, preferred_region=preferred_region, workspace_id=workspace_id
    )
    emit_json(_share_to_json(info, include_token=True))


@shares.command(name="delete")
@click.argument("host_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def delete_share(host_id: str, account: str | None, connector_url: str | None) -> None:
    """Disable sharing for the given workspace host id (revokes its relay token)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.delete_share(token, host_id)
    emit_json({"host_id": host_id, "state": "inactive"})


@shares.command(name="status")
@click.argument("host_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def share_status(host_id: str, account: str | None, connector_url: str | None) -> None:
    """Show one share's status (state, relay endpoints, per-relay tunnel login stamps, cert expiry)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    info = client.get_share_status(token, host_id)
    if info is None:
        emit_json({"host_id": host_id, "state": "none"})
        return
    emit_json(_share_to_json(info, include_token=False))


@shares.command(name="list")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_shares(account: str | None, connector_url: str | None) -> None:
    """List all of this account's share records (active and inactive)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_shares(token)
    emit_json([_share_to_json(entry, include_token=False) for entry in items])


@shares.command(name="relays")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_share_relays(account: str | None, connector_url: str | None) -> None:
    """Show the relay fleet (region -> tunnel-control endpoints)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    relay_map = client.list_share_relays(token)
    emit_json(
        {"relays": {region: list(endpoints) for region, endpoints in relay_map.relay_endpoints_by_region.items()}}
    )
