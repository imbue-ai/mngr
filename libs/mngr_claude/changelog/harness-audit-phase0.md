- `ensure_chat_cancel_tap_keybinding` no longer crashes on a keybindings.json whose Chat entry has a non-object `"bindings"` value: the malformed value is replaced with a fresh dict (with a warning logged), mirroring the existing handling of a non-list top-level `"bindings"`. Added a unit test for the non-dict case. This also fixes the type error the old `setdefault` call produced.

- `is_tap_binding_active` now logs a warning when keybindings.json is malformed JSON instead of silently returning False (still never raises), satisfying the silent-decode-error ratchet.

- Sorted imports and reformatted `claude_config.py` / `claude_config_test.py` with ruff.
