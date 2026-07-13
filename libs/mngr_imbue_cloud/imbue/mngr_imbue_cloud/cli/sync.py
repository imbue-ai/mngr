"""`mngr imbue_cloud sync ...` subcommands.

Pure transport for the workspace-sync feature: workspace records (plaintext
metadata + an opaque client-encrypted secrets blob) and the per-account
password-wrapped key bundle. These verbs exist for the minds desktop client;
the plugin never encrypts, decrypts, or interprets the secret payloads.
"""

import json
import sys
from pathlib import Path

import click
from loguru import logger

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.data_types import SyncKeyBundle
from imbue.mngr_imbue_cloud.data_types import SyncWorkspaceRecord
from imbue.mngr_imbue_cloud.errors import ImbueCloudSyncConflictError


def _read_json_payload(model_name: str, input_file: str | None) -> dict[str, object]:
    """Parse one JSON object from ``--input-file`` (preferred) or stdin.

    minds passes a 0600 temp file so the payload never rides a command line;
    stdin remains for direct human/scripted invocations.
    """
    if input_file is not None:
        try:
            raw = Path(input_file).read_text()
        except OSError as exc:
            fail_with_json(
                f"could not read --input-file for {model_name}: {exc}", error_class="UsageError", exit_code=2
            )
    else:
        raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Rejecting non-JSON input for {}: {}", model_name, exc)
        fail_with_json(f"input is not valid JSON for {model_name}: {exc}", error_class="UsageError", exit_code=2)
    if not isinstance(parsed, dict):
        fail_with_json(f"input must be a JSON object for {model_name}", error_class="UsageError", exit_code=2)
    return parsed


@click.group(name="sync")
def sync() -> None:
    """Workspace-record and key-bundle sync (transport for the minds app)."""


@sync.group(name="records")
def records() -> None:
    """Push / pull workspace records."""


@records.command(name="pull")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def pull_records(account: str | None, connector_url: str | None) -> None:
    """List all of this account's workspace records. Emits {records: [...]}."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_sync_records(token)
    emit_json({"records": [item.model_dump(mode="json") for item in items]})


@records.command(name="push")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@click.option("--input-file", default=None, help="Read the record JSON from this file instead of stdin")
@handle_imbue_cloud_errors
def push_record(account: str | None, connector_url: str | None, input_file: str | None) -> None:
    """Push one workspace record (JSON on stdin or --input-file, CAS on revision). Emits the stored record.

    On a revision conflict the JSON error body carries the server's current
    row under ``stored`` so the caller can merge and retry.
    """
    payload = _read_json_payload("a workspace record", input_file)
    try:
        record = SyncWorkspaceRecord.model_validate(payload)
    except ValueError as exc:
        fail_with_json(f"invalid workspace record: {exc}", error_class="UsageError", exit_code=2)
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    try:
        stored = client.put_sync_record(token, record)
    except ImbueCloudSyncConflictError as exc:
        fail_with_json(str(exc), error_class=type(exc).__name__, stored=exc.stored_record)
    emit_json(stored.model_dump(mode="json"))


@records.command(name="delete")
@click.argument("host_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def delete_record(host_id: str, account: str | None, connector_url: str | None) -> None:
    """Remove one workspace record outright (disassociation; idempotent)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.delete_sync_record(token, host_id)
    emit_json({"status": "deleted", "host_id": host_id})


@sync.command(name="scrub-secrets")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def scrub_secrets(account: str | None, connector_url: str | None) -> None:
    """Strip encrypted secrets from every record of this account (clear-password flow)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    scrubbed = client.scrub_sync_secrets(token)
    emit_json({"scrubbed": scrubbed})


@sync.group(name="bundle")
def bundle() -> None:
    """Push / pull / delete the account's password-wrapped key bundle."""


@bundle.command(name="pull")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def pull_bundle(account: str | None, connector_url: str | None) -> None:
    """Fetch the account's key bundle. Emits {bundle: {...}} or {bundle: null}."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    fetched = client.get_key_bundle(token)
    emit_json({"bundle": fetched.model_dump(mode="json") if fetched is not None else None})


@bundle.command(name="push")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@click.option("--input-file", default=None, help="Read the bundle JSON from this file instead of stdin")
@handle_imbue_cloud_errors
def push_bundle(account: str | None, connector_url: str | None, input_file: str | None) -> None:
    """Store (replace) the account's key bundle (JSON on stdin or --input-file)."""
    payload = _read_json_payload("a key bundle", input_file)
    try:
        parsed_bundle = SyncKeyBundle.model_validate(payload)
    except ValueError as exc:
        fail_with_json(f"invalid key bundle: {exc}", error_class="UsageError", exit_code=2)
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.put_key_bundle(token, parsed_bundle)
    emit_json({"status": "ok"})


@bundle.command(name="delete")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def delete_bundle(account: str | None, connector_url: str | None) -> None:
    """Delete the account's key bundle (idempotent; clear-password flow)."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    client.delete_key_bundle(token)
    emit_json({"status": "deleted"})
