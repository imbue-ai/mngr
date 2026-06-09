## Remove Modal async-permission-propagation workaround

Modal has fixed the bug on their side where a just-created environment returned
`modal.exception.PermissionDeniedError` for several seconds (async per-user
permission propagation) before the creating user could operate on it.
Read-after-write is now immediate, so the workaround is no longer needed.

Removed from `modal_proxy`:

- The `ModalProxyPermissionDeniedError` error class (`imbue.modal_proxy.errors`).
- The `_translate_modal_error` branch that mapped
  `modal.exception.PermissionDeniedError` to it (`imbue.modal_proxy.direct`);
  permission-denied errors once again fall through to the base `ModalProxyError`.
</content>
