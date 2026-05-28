# ty 0.0.39 suppression syntax

- Converted `# type: ignore[call-overload]` to `# ty: ignore[no-matching-overload]` in the `expect` test, as required by `ty` 0.0.39 (which no longer honors the bracketed mypy-style form).

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

Test-only change; no user-facing behavior change.
