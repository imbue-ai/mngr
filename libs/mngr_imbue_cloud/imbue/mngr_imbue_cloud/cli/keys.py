"""`mngr imbue_cloud keys litellm ...` subcommands.

Other key types (e.g. registry tokens) can be added as sibling subgroups in
the future without breaking existing usage of ``keys litellm``.
"""

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


@click.group(name="keys")
def keys() -> None:
    """Manage external service keys (LiteLLM today)."""


@keys.group(name="litellm")
def litellm() -> None:
    """LiteLLM virtual key management."""


@litellm.command(name="create")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--alias", default=None, help="Optional human-readable alias for the key")
@click.option("--max-budget", default=None, type=float, help="Max spend in USD")
@click.option("--budget-duration", default=None, help="Budget reset duration (e.g. '1d', '30d')")
@click.option(
    "--metadata",
    default=None,
    help="JSON-encoded dict of metadata to attach to the key (e.g. agent_id=...)",
)
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def create_key(
    account: str | None,
    alias: str | None,
    max_budget: float | None,
    budget_duration: str | None,
    metadata: str | None,
    connector_url: str | None,
) -> None:
    """Create a new LiteLLM virtual key. Emits {key, base_url} on stdout."""
    metadata_dict: dict[str, str] | None = None
    if metadata is not None:
        try:
            parsed = _json.loads(metadata)
        except _json.JSONDecodeError as exc:
            logger.error("Invalid --metadata JSON: {}", exc)
            fail_with_json(f"Invalid --metadata JSON: {exc}", error_class="UsageError")
        if not isinstance(parsed, dict):
            fail_with_json("--metadata must be a JSON object", error_class="UsageError")
        metadata_dict = {str(k): str(v) for k, v in parsed.items()}

    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    material = client.create_litellm_key(
        access_token=token,
        key_alias=alias,
        max_budget=max_budget,
        budget_duration=budget_duration,
        metadata=metadata_dict,
    )
    emit_json(
        {
            "key": material.key.get_secret_value(),
            "base_url": str(material.base_url),
        }
    )


@litellm.command(name="list")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def list_keys(account: str | None, connector_url: str | None) -> None:
    """List virtual keys owned by this account."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_litellm_keys(token)
    emit_json([item.model_dump(mode="json") for item in items])


@litellm.command(name="show")
@click.argument("key_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def show_key(key_id: str, account: str | None, connector_url: str | None) -> None:
    """Show metadata for a specific key id."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    info = client.get_litellm_key_info(token, key_id)
    emit_json(info.model_dump(mode="json"))


@litellm.command(name="budget")
@click.argument("key_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--max-budget", default=None, type=float, required=True, help="New max budget in USD")
@click.option("--budget-duration", default=None, help="New budget reset duration (optional)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def update_budget(
    key_id: str,
    account: str | None,
    max_budget: float | None,
    budget_duration: str | None,
    connector_url: str | None,
) -> None:
    """Update the budget for a virtual key."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.update_litellm_key_budget(
        access_token=token,
        key_id=key_id,
        max_budget=max_budget,
        budget_duration=budget_duration,
    )
    emit_json({"updated": True, "key_id": key_id, "max_budget": max_budget})


@litellm.command(name="delete")
@click.argument("key_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def delete_key(key_id: str, account: str | None, connector_url: str | None) -> None:
    """Delete a virtual key."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.delete_litellm_key(token, key_id)
    emit_json({"deleted": True, "key_id": key_id})
