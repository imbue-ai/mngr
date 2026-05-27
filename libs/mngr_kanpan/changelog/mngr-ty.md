# ty 0.0.39 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]` (test file), as required by `ty` 0.0.39.
- `_submit_batch_item` now dispatches on the command type with a `match` statement (`case MarkableBuiltinCommand()/ActionBuiltinCommand()/CustomCommand()`, with a `case _: assert_never(item.cmd)` catch-all) instead of an `isinstance` chain. This narrows `item.cmd` to `CustomCommand` before reading `.command` (which ty could not prove via the previous structure) and makes exhaustiveness explicit. Behavior is unchanged.
- Documented the urwid `Widget` -> `Text` downcast on a row's name cell with a `# ty: ignore[invalid-assignment]` (the first column is always a `Text` by construction, but urwid types `.contents` only as `Widget`).

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.
