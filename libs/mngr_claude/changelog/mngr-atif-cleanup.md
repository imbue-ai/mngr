The claude silent-decode ratchet entry now counts `common_transcript_convert.py`'s decode catches (with the justification recorded alongside them) instead of excluding the whole file, matching its codex and antigravity siblings. The file stays waived only for the rules that genuinely do not apply to a standalone stdlib-only script (bare print, `__init__` methods).

A claude release test that still counted user turns by the retired `user_message` record type now counts ATIF user steps.

The `emit_common_transcript` config field's description now describes the ATIF-shaped stream (user turns, agent turns, tool results) rather than the retired record vocabulary.

Trimmed historical framing from the converter's module docstring.
