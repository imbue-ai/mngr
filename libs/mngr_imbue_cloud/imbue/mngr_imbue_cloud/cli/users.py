"""`mngr imbue_cloud users ...` subcommands: identity-record and public-profile lookups."""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token


@click.group(name="users")
def users() -> None:
    """Look up other users' identity records (id, verified email, display name, profile picture)."""


@users.command(name="show")
@click.argument("user_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def show_user(user_id: str, account: str | None, connector_url: str | None) -> None:
    """Print the identity record of the user with USER_ID (error code user_not_found when unknown)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    emit_json(client.get_user(token, user_id).model_dump(mode="json"))


@users.command(name="profile")
@click.argument("user_id")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def show_public_profile(user_id: str, connector_url: str | None) -> None:
    """Print the public profile (display_name, profile_picture_url) of the user with USER_ID.

    Needs no account: the connector serves it unauthenticated, and answers
    nulls rather than an error for an id it has no profile for.
    """
    client = make_connector_client(connector_url)
    emit_json(client.get_public_profile(user_id).model_dump(mode="json"))


@users.command(name="resolve")
@click.argument("email")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def resolve_user(email: str, account: str | None, connector_url: str | None) -> None:
    """Print the identity record whose verified email is EMAIL.

    Fails with code ``user_not_found`` when no verified account matches and
    ``rate_limited`` when the caller's hourly lookup budget is spent
    (lookups of existing contacts never count against it).
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    emit_json(client.resolve_user_by_email(token, email).model_dump(mode="json"))
