"""`mngr imbue_cloud admin account ...` -- operator-only per-account entitlements management.

Addressed by *email* (the connector resolves the SuperTokens user); all
commands authenticate with the fixed ``MINDS_ADMIN_KEY`` API key, like
the paid-list CRUD. ``show`` lazily materializes the account's entitlements
row; ``set-plan`` resets the row wholesale to the plan's defaults (the way to
wipe manual bumps -- it deliberately skips the ally eligibility check);
``set-quota`` bumps a single entitlement value.
"""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli.paid import paid_auth_options
from imbue.mngr_imbue_cloud.cli.paid import resolve_admin_api_key


@click.group(name="account")
def account_admin() -> None:
    """Show / set plans and quotas for a user account (requires MINDS_ADMIN_KEY)."""


@account_admin.command(name="show")
@click.argument("email")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_show_account(email: str, connector_url: str | None, api_key: str | None) -> None:
    """Show one account's plan, entitlement values, and live usage."""
    client = make_connector_client(connector_url)
    info = client.admin_get_account(resolve_admin_api_key(api_key), email)
    emit_json(info.model_dump(mode="json"))


@account_admin.command(name="set-plan")
@click.argument("email")
@click.argument("plan")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_set_plan(email: str, plan: str, connector_url: str | None, api_key: str | None) -> None:
    """Assign PLAN to the account, resetting its entitlements to the plan's defaults."""
    client = make_connector_client(connector_url)
    emit_json(client.admin_set_account_plan(resolve_admin_api_key(api_key), email, plan))


@account_admin.command(name="set-quota")
@click.argument("email")
@click.argument("entitlement")
@click.argument("value", type=float)
@paid_auth_options
@handle_imbue_cloud_errors
def admin_set_quota(
    email: str, entitlement: str, value: float, connector_url: str | None, api_key: str | None
) -> None:
    """Set a single entitlement VALUE on the account (an operator bump).

    ENTITLEMENT is one of: max_remote_workspaces, max_tunnels,
    max_services_per_tunnel, max_buckets, max_total_bucket_bytes,
    monthly_llm_spend_usd, max_active_synced_workspaces.
    """
    client = make_connector_client(connector_url)
    emit_json(client.admin_set_account_quota(resolve_admin_api_key(api_key), email, entitlement, value))
