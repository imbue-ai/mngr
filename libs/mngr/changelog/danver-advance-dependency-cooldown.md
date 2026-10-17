The repo-wide dependency refresh advanced the `[tool.uv] exclude-newer` supply-chain cooldown cutoff to the policy maximum (UTC today minus two weeks, 2026-09-02) and re-locked with `uv lock --upgrade`, so every dependency now sits at the newest release the cooldown admits rather than whatever was previously pinned. 136 packages moved.

`mngr`'s own code follows the annotations the refreshed libraries tightened, with no behaviour change:

- click 8.5 deprecates `get_text_stream` (removal in click 9.0). The error renderer writes to `sys.stderr` directly, which is the same stream the helper returned.

- urwid 4.0.13 types a key as `str | tuple[str, int, int, int]` -- the 4-tuple is a mouse event -- so the `mngr extras` picker and the plugin-install wizard's input filters accept and return that union. It also types a `ListBox` position as `SupportsIndex`, which the single-select picker converts before returning an index.
