# ty 0.0.39 suppression syntax

- Converted the `RecursivePluginConfig.merge_with` override suppression from `# type: ignore[override]` to `# ty: ignore[invalid-method-override]`, as required by `ty` 0.0.39 (which no longer honors the bracketed mypy-style form).

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.
