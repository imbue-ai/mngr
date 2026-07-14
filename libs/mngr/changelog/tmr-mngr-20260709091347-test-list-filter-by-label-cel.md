Tests: fixed the LABELS/filtering e2e tutorial test `test_list_filter_by_label_cel` so it verifies exactly its scope (the `mngr list --include` CEL label filter keeps `labels.priority == "high"` and drops the rest).

Two fixes were needed:

- The shared e2e fixture now restricts provider discovery to the backends a test actually declares via its markers (always `local`, plus `modal`/`docker` when the test carries that marker). Previously every installed provider plugin got a default instance during `mngr list`, so an unreachable backend a local-only test never exercises -- AWS with no credentials, or Docker with no running daemon -- surfaced as `ProviderUnavailableError` and made a plain `mngr list` exit 6 (PROVIDER_INACCESSIBLE).

- Dropped the superfluous `@pytest.mark.rsync` from `test_list_filter_by_label_cel`: it creates only local command agents, and rsync is a remote-only file transfer, so the resource guard correctly flagged the mark as never exercised.
