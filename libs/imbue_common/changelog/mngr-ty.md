# ty 0.0.39 suppression syntax

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]`, as required by `ty` 0.0.39 (which no longer honors the mypy-style bracketed form). Affected: the `field_ref` proxy returns in `frozen_model`/`mutable_model`, the `entry_points` cache monkeypatch in `conftest_hooks`, and an event-level assignment in the event-envelope test.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.
