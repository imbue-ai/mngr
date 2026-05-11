"""`mngr imbue_cloud tunnels ...` subcommands."""

import json as _json

import click
from loguru import logger

from imbue.mngr_imbue_cloud.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.data_types import AuthPolicy


@click.group(name="tunnels")
def tunnels() -> None:
    """Manage Cloudflare tunnels for forwarded agent services."""


def _parse_policy_arg(policy_json: str | None) -> AuthPolicy | None:
    if policy_json is None:
        return None
    try:
        parsed = _json.loads(policy_json)
    except _json.JSONDecodeError as exc:
        logger.error("Invalid --policy JSON: {}", exc)
        fail_with_json(f"Invalid --policy JSON: {exc}", error_class="UsageError")
    if not isinstance(parsed, dict):
        fail_with_json("--policy must be a JSON object", error_class="UsageError")
    return AuthPolicy.model_validate(parsed)


@tunnels.command(name="create")
@click.argument("agent_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option(
    "--policy",
    default=None,
    help='Default Cloudflare Access policy as JSON, e.g. \'{"emails":["a@example.com"]}\'',
)
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def create_tunnel(agent_id: str, account: str | None, policy: str | None, connector_url: str | None) -> None:
    """Create a Cloudflare tunnel for the given agent."""
    parsed_policy = _parse_policy_arg(policy)
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    info = client.create_tunnel(token, agent_id, parsed_policy)
    emit_json(
        {
            "tunnel_name": info.tunnel_name,
            "tunnel_id": info.tunnel_id,
            "token": info.token.get_secret_value() if info.token else None,
            "services": list(info.services),
        }
    )


@tunnels.command(name="list")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_tunnels(account: str | None, connector_url: str | None) -> None:
    """List all tunnels owned by this account."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_tunnels(token)
    emit_json(
        [
            {
                "tunnel_name": entry.tunnel_name,
                "tunnel_id": entry.tunnel_id,
                "services": list(entry.services),
            }
            for entry in items
        ]
    )


@tunnels.command(name="delete")
@click.argument("tunnel_name")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def delete_tunnel(tunnel_name: str, account: str | None, connector_url: str | None) -> None:
    """Delete a tunnel and cascade-clean its DNS, ingress, and KV entries."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.delete_tunnel(token, tunnel_name)
    emit_json({"deleted": True, "tunnel_name": tunnel_name})


@tunnels.group(name="services")
def services() -> None:
    """Manage forwarded services on a tunnel."""


@services.command(name="add")
@click.argument("tunnel_name")
@click.argument("service_name")
@click.argument("service_url")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def add_service(
    tunnel_name: str,
    service_name: str,
    service_url: str,
    account: str | None,
    connector_url: str | None,
) -> None:
    """Add a service to a tunnel (creates DNS + Access app)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    info = client.add_service(token, tunnel_name, service_name, service_url)
    emit_json(
        {
            "service_name": info.service_name,
            "service_url": info.service_url,
            "hostname": info.hostname,
        }
    )


@services.command(name="list")
@click.argument("tunnel_name")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_services(tunnel_name: str, account: str | None, connector_url: str | None) -> None:
    """List services configured on a tunnel."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_services(token, tunnel_name)
    emit_json([entry.model_dump(mode="json") for entry in items])


@services.command(name="remove")
@click.argument("tunnel_name")
@click.argument("service_name")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def remove_service(
    tunnel_name: str,
    service_name: str,
    account: str | None,
    connector_url: str | None,
) -> None:
    """Remove a service (deletes its DNS + Access app)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.remove_service(token, tunnel_name, service_name)
    emit_json({"removed": True, "tunnel_name": tunnel_name, "service_name": service_name})


@tunnels.group(name="auth")
def tunnel_auth() -> None:
    """Manage default and per-service auth policies."""


@tunnel_auth.command(name="get")
@click.argument("tunnel_name")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option(
    "--service",
    default=None,
    help="If set, fetch the policy for this service instead of the tunnel default",
)
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def get_auth(
    tunnel_name: str,
    account: str | None,
    service: str | None,
    connector_url: str | None,
) -> None:
    """Get the default tunnel policy or the policy for a specific service."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    if service is None:
        policy = client.get_tunnel_auth(token, tunnel_name)
    else:
        policy = client.get_service_auth(token, tunnel_name, service)
    emit_json(policy.model_dump(mode="json"))


@tunnel_auth.command(name="set")
@click.argument("tunnel_name")
@click.argument("policy_json")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option(
    "--service",
    default=None,
    help="If set, set the policy for this service instead of the tunnel default",
)
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def set_auth(
    tunnel_name: str,
    policy_json: str,
    account: str | None,
    service: str | None,
    connector_url: str | None,
) -> None:
    """Set the default tunnel policy or the policy for a specific service.

    POLICY_JSON is a JSON object with optional emails / email_domains / require_idp keys.
    """
    parsed_policy = _parse_policy_arg(policy_json)
    # _parse_policy_arg returns None only when its input is None; we passed a
    # required positional argument, so this is just a safety belt for the type
    # checker.
    if parsed_policy is None:
        fail_with_json("Policy cannot be empty", error_class="UsageError")
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    if service is None:
        client.set_tunnel_auth(token, tunnel_name, parsed_policy)
    else:
        client.set_service_auth(token, tunnel_name, service, parsed_policy)
    emit_json({"updated": True, "tunnel_name": tunnel_name, "service": service})
