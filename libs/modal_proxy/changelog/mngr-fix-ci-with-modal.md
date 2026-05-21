## Translate Modal `PermissionDeniedError`

Modal recently migrated their permission system so that the per-user permission entry for a just-created environment is propagated asynchronously (typically ~3-7 seconds after `modal environment create` returns success). During that window, any operation on the new environment raises `modal.exception.PermissionDeniedError` instead of the previous `NotFoundError`.

- `imbue.modal_proxy.errors`: new `ModalProxyPermissionDeniedError`.
- `imbue.modal_proxy.direct._translate_modal_error`: maps `modal.exception.PermissionDeniedError` to the new typed error (previously it fell through to the bare `ModalProxyError`).
