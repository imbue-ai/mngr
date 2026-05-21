## Retry on async Modal permission propagation

Modal recently migrated their permission system so the per-user permission entry for a just-created environment is propagated asynchronously (typically ~3-7 seconds after `modal environment create` returns success). During that window, operations on the new environment raise `modal.exception.PermissionDeniedError` instead of `NotFoundError`. This was breaking every Modal acceptance test at fixture construction time.

- `imbue.mngr_modal.backend`: both `_enter_ephemeral_app_context_with_retry` and `_lookup_persistent_app_with_retry` now retry on `ModalProxyPermissionDeniedError` in addition to `ModalProxyNotFoundError`, matching the existing 5-attempt exponential backoff (1s→10s) used for env-not-found.
- `libs/mngr_modal/imbue/mngr_modal/conftest.py`: the test cleanup helper `_classify_modal_sdk_delete` now retries Modal SDK deletes through the same propagation window so that fast-running test teardowns don't spuriously leak environments/volumes.

No user-visible behavior change beyond fewer transient Modal failures and a small added startup latency (~3-7s on the very first `mngr create` against a brand-new environment, only on first use per profile).
