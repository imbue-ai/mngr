"""`mngr imbue_cloud bucket ...` subcommands.

Manage R2 buckets (one per host the user makes) and their scoped S3 keys.
Credentials are emitted as JSON; the secret access key is shown only once at
creation time and is never persisted by the connector.
"""

from collections.abc import Callable

import click

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.data_types import R2KeyMaterial
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotEmptyError
from imbue.mngr_imbue_cloud.r2_objects import empty_bucket_for_destroy


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


def _destroy_emptying_on_refusal(
    destroy_bucket_once: Callable[[], None],
    empty_bucket: Callable[[], int],
    is_force: bool,
) -> int:
    """Destroy the bucket, emptying it only after a not-empty refusal (with force).

    The destroy is always attempted FIRST so server-side refusals that must
    precede any data loss -- in particular the active-workspace interlock on
    workspace-backup buckets -- are hit before a single object is deleted.
    Only the specific not-empty refusal triggers the client-side emptying,
    after which the destroy is retried. Returns the number of objects deleted
    (0 when the bucket was already empty).
    """
    try:
        destroy_bucket_once()
    except ImbueCloudBucketNotEmptyError:
        if not is_force:
            raise
        emptied_object_count = empty_bucket()
        destroy_bucket_once()
        return emptied_object_count
    return 0


@bucket.command(name="destroy")
@click.argument("name")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="When the destroy is refused as non-empty, delete the bucket's contents (batched S3 deletes) and retry",
)
@click.option("--yes", "-y", "is_confirmed", is_flag=True, default=False, help="Skip the --force confirmation prompt")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def destroy_bucket(name: str, force: bool, is_confirmed: bool, account: str | None, connector_url: str | None) -> None:
    """Destroy a bucket (refuses if non-empty) and revoke all of its keys.

    With --force, a bucket whose destroy is refused as non-empty has every
    object deleted (client-side, over the S3 API, so no long-running server
    request) and the destroy is retried. The destroy is always attempted
    first, so the connector's refusal to destroy a workspace-backup bucket
    whose workspace is active arrives before any contents are deleted,
    --force or not.
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    if force and not is_confirmed:
        click.confirm(
            f"Delete ALL contents of bucket '{name}' and destroy it? This cannot be undone.",
            abort=True,
        )
    emptied_object_count = _destroy_emptying_on_refusal(
        destroy_bucket_once=lambda: client.destroy_bucket(token, name),
        empty_bucket=lambda: empty_bucket_for_destroy(client, token, name),
        is_force=force,
    )
    emit_json({"destroyed": True, "bucket": name, "emptied_object_count": emptied_object_count})


@bucket.command(name="roll-key")
@click.argument("name")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def roll_key(name: str, account: str | None, connector_url: str | None) -> None:
    """Roll the bucket's single key: same Access Key ID, fresh secret. Emits the key material.

    Each bucket has exactly one key; the secret is shown only once, so this
    is how you get working credentials again (and how a leaked secret is
    invalidated -- the old value stops working immediately).
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    material = client.roll_bucket_key(access_token=token, name=name)
    emit_json(_key_material_to_json(material))


@bucket.group(name="keys")
def keys() -> None:
    """Inspect the S3 keys for a bucket (each bucket has a single key; see `bucket roll-key`)."""


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
