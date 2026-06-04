Strengthened the modal_proxy test suite so it catches regressions in the Modal error-translation and retry boundary that previously went untested:

- `_translate_modal_error` is now exercised on all eight branches (auth, permission-denied, not-found, invalid, internal, resource-exhausted, remote, and the generic fallback), asserting the exact `ModalProxy*` type and that the original message survives translation. A swapped, dropped, or reordered `isinstance` mapping is now caught.
- The volume retry predicate now verifies that `StreamTerminatedError` and `ProtocolError` (transient connection failures) are retried, so silently dropping them from the retry set would fail.
- Added direct unit tests for `is_environment_not_found_error` covering the empty-name, no-quotes, leading-text, path-containing-"Environment", and empty-string edges, pinning the regex that drives the retry decision.
- The stream-type and file-entry-type converters now test their unsupported-value default arm, so deleting or weakening it is caught.
- The unwrap helpers now wrap genuinely-validated `Direct*` objects around real `modal.Image`/`App`/`Volume`/`Secret` instances instead of sentinel-filled `model_construct` shells, exercising real field validation.
