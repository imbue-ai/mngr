"""`mngr imbue_cloud sync ...` subcommands.

Pure transport for the workspace-sync feature: workspace records (plaintext
metadata + an opaque client-encrypted secrets blob), the per-account
password-wrapped key bundle, and per-account documents (plaintext metadata +
an opaque client-encrypted payload, e.g. cloud connections). These verbs exist
for the minds desktop client; the plugin never encrypts, decrypts, or
interprets the secret payloads or the document metadata.
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
from imbue.mngr_imbue_cloud.errors import ImbueCloudSyncConflictError
from imbue.mngr_imbue_cloud.wire_types import SyncDocument
from imbue.mngr_imbue_cloud.wire_types import SyncKeyBundle
from imbue.mngr_imbue_cloud.wire_types import SyncWorkspaceRecord


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
    """Workspace-record, key-bundle, and account-document sync (transport for the Imbue Studio app)."""


@sync.group(name="records")
def records() -> None:
    """Push / pull workspace records."""


@records.command(name="pull")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def pull_records(account: str | None, connector_url: str | None) -> None:
    """List all of this account's workspace records. Emits {records: [...], shared_agent_ids: [...]}.

    ``shared_agent_ids`` names the workspaces (record ``agent_id``) this
    account actively shares; it is empty against a connector too old to report it.
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    emit_json(client.list_sync_records_with_shares(token).model_dump(mode="json"))


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
@click.argument("record_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def delete_record(record_id: str, account: str | None, connector_url: str | None) -> None:
    """Remove one workspace record outright (disassociation; idempotent).

    RECORD_ID is the workspace id (``agent-<hex>``, preferred -- the record's
    durable key) or, for compatibility, the workspace's current host id
    (``host-<hex>``).

    Refused (connector 409 ``lease_active``) while the workspace still holds
    its cloud lease: destroy the workspace instead, which releases the lease
    and retires the record.
    """
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    if record_id.startswith("agent-"):
        client.delete_sync_record_by_workspace(token, record_id)
    else:
        client.delete_sync_record(token, record_id)
    emit_json({"status": "deleted", "record_id": record_id})


@sync.command(name="scrub-secrets")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def scrub_secrets(account: str | None, connector_url: str | None) -> None:
    """Strip every encrypted record secret and document payload of this account (clear-password flow).

    Emits ``{scrubbed: N}``, the number of records scrubbed; document payloads
    are cleared by the same call.
    """
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


@sync.group(name="documents")
def documents() -> None:
    """Push / pull / delete per-account documents (e.g. cloud connections)."""


@documents.command(name="pull")
@click.option("--kind", required=True, help="Document kind to list (e.g. connection)")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def pull_documents(kind: str, account: str | None, connector_url: str | None) -> None:
    """List every document of one kind, tombstones included. Emits {documents: [...]}."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    items = client.list_sync_documents(token, kind)
    emit_json({"documents": [item.model_dump(mode="json") for item in items]})


@documents.command(name="push")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@click.option("--input-file", default=None, help="Read the document JSON from this file instead of stdin")
@handle_imbue_cloud_errors
def push_document(account: str | None, connector_url: str | None, input_file: str | None) -> None:
    """Push one document (JSON on stdin or --input-file; revision 1 inserts, else CAS). Emits the stored document.

    Only the fields present in the input are sent: leave ``encrypted_payload``
    out to keep the stored payload, or send it as null to clear it. On a
    revision conflict the JSON error body carries the server's current row
    under ``stored`` so the caller can merge and retry.
    """
    payload = _read_json_payload("an account document", input_file)
    try:
        document = SyncDocument.model_validate(payload)
    except ValueError as exc:
        fail_with_json(f"invalid account document: {exc}", error_class="UsageError", exit_code=2)
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    try:
        stored = client.put_sync_document(token, document)
    except ImbueCloudSyncConflictError as exc:
        fail_with_json(str(exc), error_class=type(exc).__name__, stored=exc.stored_record)
    emit_json(stored.model_dump(mode="json"))


@documents.command(name="delete")
@click.argument("kind")
@click.argument("doc_id")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def delete_document(kind: str, doc_id: str, account: str | None, connector_url: str | None) -> None:
    """Tombstone one document (idempotent). Emits the stored tombstone."""
    client = make_connector_client(connector_url)
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    token = get_active_token(store, client, parsed_account)
    stored = client.delete_sync_document(token, kind, doc_id)
    emit_json(stored.model_dump(mode="json"))
