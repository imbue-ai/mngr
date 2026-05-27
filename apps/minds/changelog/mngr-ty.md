# ty 0.0.39 type fix

- `_resolve_ws_name_and_account` now returns `list[AccountSession]` instead of `list[object]`. `ty` 0.0.39 rejected the previous annotation because `list` is invariant (`list[AccountSession]` is not assignable to `list[object]`); the precise element type is also more accurate.

No user-facing behavior change.
