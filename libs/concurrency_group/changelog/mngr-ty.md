# ty 0.0.39 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]`, as required by `ty` 0.0.39.
- Reworked the exit-path exception handling in `ConcurrencyGroup` to accumulate a typed `list[Exception]` (the non-`Exception` `BaseException`s are still re-raised exactly as before) so that the `_deduplicate_exceptions` call type-checks under the stricter checker. Behavior is unchanged.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.
