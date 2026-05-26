# ty 0.0.39 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]` (test file), as required by `ty` 0.0.39.
- `_submit_batch_item` now narrows the command with an explicit `isinstance(item.cmd, CustomCommand)` check before reading `.command`, so `ty` can prove the access is valid (it could not follow the previous match-exhaustiveness narrowing). Behavior is unchanged.
- Documented the urwid `Widget` -> `Text` downcast on a row's name cell with a `# ty: ignore[invalid-assignment]` (the first column is always a `Text` by construction, but urwid types `.contents` only as `Widget`).

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.
