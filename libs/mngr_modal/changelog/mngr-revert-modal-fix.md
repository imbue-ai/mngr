## Remove Modal async-permission-propagation workaround

Modal has fixed the bug on their side where a just-created environment returned
`modal.exception.PermissionDeniedError` for several seconds (async per-user
permission propagation) before the creating user could operate on it.
Read-after-write is now immediate, so the workaround is no longer needed.

Removed from `mngr_modal`:

- The `ModalProxyPermissionDeniedError` retries in
  `_lookup_persistent_app_with_retry` and `_enter_ephemeral_app_context_with_retry`
  (`imbue.mngr_modal.backend`); both decorators once again retry only on
  `ModalProxyNotFoundError`.
- The `_invoke_modal_sdk_delete_with_retry` test-cleanup helper in `conftest.py`;
  `_classify_modal_sdk_delete` now invokes the SDK delete callable directly again.
</content>
