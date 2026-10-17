Account-document transport (part 2 of the account-owned remote providers program, `specs/account-owned-providers/spec.md`): the plugin half of syncing an account's cloud connections across devices. Pure transport, like the record verbs: the plugin never encrypts, decrypts, or interprets documents.

- `SyncDocument` wire model and `SUPPORTED_DOCUMENT_FORMAT` in `wire_types.py`.

- `ImbueCloudConnectorClient` gains `list_sync_documents`, `put_sync_document` (sends only the fields set on the document, so an omitted `encrypted_payload` gets the server's preserve-on-absent behavior), and `delete_sync_document`. A 409 `revision_conflict` raises `ImbueCloudSyncConflictError` with the stored row; `document_format_too_new` raises `ImbueCloudRecordFormatTooNewError` (its docstring now covers both records and documents); the new `ImbueCloudDocumentLimitError` covers `document_limit_exceeded` and `ImbueCloudDocumentNotFoundError` a delete of a document the connector holds no row for.

- New `mngr imbue_cloud sync documents` subgroup: `pull --kind KIND`, `push --input-file PATH` (or stdin; a conflict emits the JSON error with `stored`, exactly like `records push`), and `delete KIND DOC_ID`, each with `--account` / `--connector-url`.

- A dev-tier acceptance test (`test_acceptance_sync_documents.py`, gated on `MNGR_IMBUE_CLOUD_ACCEPTANCE_CONNECTOR_URL` / `_EMAIL` / `_PASSWORD`) round-trips push, pull, scrub, and delete through a real connector.
