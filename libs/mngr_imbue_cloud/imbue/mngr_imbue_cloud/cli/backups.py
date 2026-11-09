"""`mngr imbue_cloud backup-repo ...` subcommands.

Manage the account's workspace backup repositories on the backup store
(specs/backup-store/spec.md): one restic repository per workspace, reached
over restic's REST backend with a per-repository token. Credentials are
emitted as JSON; the token is shown only once (at create or roll) and is
never persisted by the connector.
"""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.wire_types import BackupRepositoryCredentials


def credentials_to_json(credentials: BackupRepositoryCredentials) -> dict[str, str]:
    """Render repository credentials with the token revealed (it is only shown once)."""
    return {
        "workspace_id": credentials.workspace_id,
        "repository_url": credentials.repository_url,
        "rest_username": credentials.rest_username,
        "rest_password": credentials.rest_password.get_secret_value(),
    }


@click.group(name="backup-repo")
def backup_repo() -> None:
    """Manage workspace backup repositories on the backup store."""


@backup_repo.command(name="create")
@click.argument("workspace_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def create_backup_repository(workspace_id: str, account: str | None, connector_url: str | None) -> None:
    """Create the workspace's repository and emit its credentials (idempotent: an existing one gets a fresh token)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    credentials = client.create_backup_repository(access_token=token, workspace_id=workspace_id)
    emit_json(credentials_to_json(credentials))


@backup_repo.command(name="list")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_backup_repositories(account: str | None, connector_url: str | None) -> None:
    """List the account's backup repositories with their usage."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_backup_repositories(token)
    emit_json([item.model_dump(mode="json") for item in items])


@backup_repo.command(name="roll-token")
@click.argument("workspace_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def roll_backup_repository_token(workspace_id: str, account: str | None, connector_url: str | None) -> None:
    """Mint a fresh token for the repository (the old one stops working at once) and emit the credentials."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    credentials = client.roll_backup_repository_token(access_token=token, workspace_id=workspace_id)
    emit_json(credentials_to_json(credentials))


@backup_repo.command(name="destroy")
@click.argument("workspace_id")
@click.option("--yes", "-y", "is_confirmed", is_flag=True, default=False, help="Skip the confirmation prompt")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def destroy_backup_repository(
    workspace_id: str, is_confirmed: bool, account: str | None, connector_url: str | None
) -> None:
    """Delete the repository and every backup in it.

    The connector refuses while the workspace record is still ACTIVE --
    destroy the workspace first. Deletion is bounded per call server-side,
    so this loops until the connector reports nothing remaining.
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    if not is_confirmed:
        click.confirm(
            f"Delete ALL backups of workspace '{workspace_id}'? This cannot be undone.",
            abort=True,
        )
    client.destroy_backup_repository(access_token=token, workspace_id=workspace_id)
    emit_json({"status": "deleted"})


@backup_repo.command(name="usage")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def backup_storage_usage(account: str | None, connector_url: str | None) -> None:
    """Show the account's backup storage usage, soft and hard limits, and enforcement mode."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    usage = client.get_backup_storage_usage(token)
    emit_json(usage.model_dump(mode="json"))
