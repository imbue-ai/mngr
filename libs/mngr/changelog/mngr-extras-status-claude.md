Centralized the Claude Code CLI presence check. `mngr extras` status (`_claude_plugin_status`) and the `is_claude_installed` test helper now both defer to the canonical `CLAUDE.is_available()` system-dependency check instead of re-implementing `shutil.which("claude")` inline, so the binary name and lookup logic live in one place.

Also removed a duplicate subprocess-error tuple: `extras.py` now imports the shared `SUBPROCESS_ERRORS` from `imbue.mngr.utils.deps` (promoted from the previously private `_SUBPROCESS_ERRORS`) rather than defining its own copy.
