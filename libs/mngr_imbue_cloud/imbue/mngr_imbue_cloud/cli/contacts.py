"""`mngr imbue_cloud contacts ...` subcommands: the caller's private, one-way contacts list."""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token


@click.group(name="contacts")
def contacts() -> None:
    """Manage the account's contacts (people you have shared with, or added by hand)."""


@contacts.command(name="list")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_contacts(account: str | None, connector_url: str | None) -> None:
    """Emit one JSON object per contact (identity record plus your private trust level)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    emit_json([entry.model_dump(mode="json") for entry in client.list_contacts(token)])


@contacts.command(name="add")
@click.argument("user_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def add_contact(user_id: str, account: str | None, connector_url: str | None) -> None:
    """Add (or re-add) the user with USER_ID to your contacts; the contact is never notified."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    emit_json(client.add_contact(token, user_id).model_dump(mode="json"))


@contacts.command(name="remove")
@click.argument("user_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def remove_contact(user_id: str, account: str | None, connector_url: str | None) -> None:
    """Remove the user with USER_ID from your contacts (a no-op when absent)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.remove_contact(token, user_id)
    emit_json({"user_id": user_id, "removed": True})
