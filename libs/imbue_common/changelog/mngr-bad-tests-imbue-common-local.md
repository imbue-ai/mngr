Strengthened several weak or misleading unit tests in imbue_common:

- `pytest_utils`: split the snapshot-flag parsing into a pure `is_updating_for_inline_snapshot_flags` helper and replaced the no-op "demo" test (whose assertion could never fail) with a parametrized test over real flag values.
- `setup_logging` tests now assert the configured level actually filters/emits messages instead of only checking that no exception is raised.
- `EventEnvelope` tests now verify that a missing field is rejected and that serialization emits the exact field values (not just key presence).
- `primitives`: added pydantic-validation rejection cases for every constrained primitive (previously only happy-path values were tested).
- `detect_branch` git fallback is now tested deterministically against a real repo on a unique branch instead of conditionally skipping its assertion.
- Tightened ratchet-testing assertions (exact matched-content set, error-message matching), renamed undescriptive tests, and made the package-import smoke test meaningful.
