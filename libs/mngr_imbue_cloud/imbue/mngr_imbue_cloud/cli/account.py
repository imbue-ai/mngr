"""`mngr imbue_cloud account ...` subcommands.

Shows the signed-in account's plan, entitlement values, and live usage, and
lets the user switch plans. Quota enforcement itself happens server-side in
the connector; these commands are the CLI view of it.
"""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token


@click.group(name="account")
def account() -> None:
    """Show the account's plan, quotas, and usage; switch plans."""


@account.command(name="show")
@click.option("--account", "account_email", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def show_account(account_email: str | None, connector_url: str | None) -> None:
    """Show the plan, entitlement values, and live usage for the account."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account_email)
    token = get_active_token(store, client, parsed_account)
    info = client.get_account(token)
    emit_json(info.model_dump(mode="json"))


@account.command(name="set-plan")
@click.argument("plan")
@click.option("--account", "account_email", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def set_plan(plan: str, account_email: str | None, connector_url: str | None) -> None:
    """Switch the account to PLAN, resetting entitlements to the plan's defaults.

    Re-selecting the current plan is a no-op. Switching to 'ally' requires a
    paid-listed email (the connector enforces this and errors with the
    reason).
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account_email)
    token = get_active_token(store, client, parsed_account)
    emit_json(client.set_account_plan(token, plan))


@account.command(name="cleanup-grant")
@click.option("--account", "account_email", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def cleanup_grant(account_email: str | None, connector_url: str | None) -> None:
    """Temporarily restore storage-downgraded bucket keys so restic cleanup can run.

    When the account is over its storage quota, its bucket keys are read-only;
    restic's space reclaim (forget + prune) needs write access. The grant
    restores the keys; run `account recheck-storage` after cleaning up to
    settle it (a grant that frees no space at all counts against a small
    rolling budget). Idempotent: safe to re-run while a grant is active.
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account_email)
    token = get_active_token(store, client, parsed_account)
    emit_json(client.create_storage_cleanup_grant(token).model_dump(mode="json"))


@account.command(name="recheck-storage")
@click.option("--account", "account_email", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def recheck_storage(account_email: str | None, connector_url: str | None) -> None:
    """Re-measure live storage usage and apply enforcement immediately.

    Restores keys as soon as usage is back under quota (no waiting for the
    hourly sweep) and settles an outstanding cleanup grant.
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account_email)
    token = get_active_token(store, client, parsed_account)
    emit_json(client.recheck_storage(token).model_dump(mode="json"))
