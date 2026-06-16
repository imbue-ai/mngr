"""`mngr imbue_cloud bucket ...` subcommands.

Manage R2 buckets (one per host the user makes) and their scoped S3 keys.
Credentials are emitted as JSON; the secret access key is shown only once at
creation time and is never persisted by the connector.
"""

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.data_types import R2KeyMaterial


def _key_material_to_json(material: R2KeyMaterial) -> dict[str, str]:
    """Render key material with the secret revealed (it is only shown once)."""
    return {
        "access_key_id": str(material.access_key_id),
        "secret_access_key": material.secret_access_key.get_secret_value(),
        "s3_endpoint": str(material.s3_endpoint),
        "bucket_name": material.bucket_name,
        "access": str(material.access),
    }


@click.group(name="bucket")
def bucket() -> None:
    """Manage R2 buckets and their scoped S3 keys."""


@bucket.command(name="create")
@click.argument("name")
@click.option(
    "--access",
    type=click.Choice(["read", "readwrite"]),
    default="readwrite",
    help="Access scope for the default key minted with the bucket",
)
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def create_bucket(name: str, access: str, account: str | None, connector_url: str | None) -> None:
    """Create a bucket and mint its default key. Emits {bucket, key} (key includes the secret)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    result = client.create_bucket(access_token=token, name=name, access=access)
    emit_json(
        {
            "bucket": result.bucket.model_dump(mode="json"),
            "key": _key_material_to_json(result.key),
        }
    )


@bucket.command(name="list")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_buckets(account: str | None, connector_url: str | None) -> None:
    """List buckets owned by this account."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_buckets(token)
    emit_json([item.model_dump(mode="json") for item in items])


@bucket.command(name="info")
@click.argument("name")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def bucket_info(name: str, account: str | None, connector_url: str | None) -> None:
    """Show metadata for a single bucket (keys come from `bucket keys list`)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    info = client.get_bucket_info(token, name)
    emit_json(info.model_dump(mode="json"))


@bucket.command(name="destroy")
@click.argument("name")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def destroy_bucket(name: str, account: str | None, connector_url: str | None) -> None:
    """Destroy a bucket (refuses if non-empty) and revoke all of its keys."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.destroy_bucket(token, name)
    emit_json({"destroyed": True, "bucket": name})


@bucket.group(name="keys")
def keys() -> None:
    """Manage the scoped S3 keys for a bucket."""


@keys.command(name="create")
@click.argument("bucket_name")
@click.option("--alias", default=None, help="Optional human-readable alias for the key")
@click.option(
    "--access",
    type=click.Choice(["read", "readwrite"]),
    default="readwrite",
    help="Access scope for the key",
)
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def create_key(
    bucket_name: str,
    alias: str | None,
    access: str,
    account: str | None,
    connector_url: str | None,
) -> None:
    """Mint an additional scoped key for a bucket. Emits the key material (includes the secret)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    material = client.create_bucket_key(access_token=token, name=bucket_name, alias=alias, access=access)
    emit_json(_key_material_to_json(material))


@keys.command(name="list")
@click.argument("bucket_name", required=False, default=None)
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_keys(bucket_name: str | None, account: str | None, connector_url: str | None) -> None:
    """List keys for one bucket, or across all buckets when no bucket is given."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_bucket_keys(token, bucket_name)
    emit_json([item.model_dump(mode="json") for item in items])


@keys.command(name="destroy")
@click.argument("access_key_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def destroy_key(access_key_id: str, account: str | None, connector_url: str | None) -> None:
    """Revoke a bucket key by its Access Key ID."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.destroy_bucket_key(token, access_key_id)
    emit_json({"destroyed": True, "access_key_id": access_key_id})
