Improved the test suite for the Telegram integration (`imbue/minds/telegram`) and fixed the issues those tests surfaced:

- Refactored `credential_extractor.py` to split the localStorage parsing/validation out of the live-browser path into pure helpers (`_parse_dc_id_and_user_id`, `_parse_auth_key_hex`, `_parse_first_name`) and added unit tests covering every branch (missing/invalid data-center ID, unparseable `user_auth`, missing user ID, bare vs JSON-quoted auth key, wrong-length key, and best-effort first-name parsing). Previously only module constants were tested.
- Extracted the `mngr exec` command construction in `injector.py` into a pure `build_inject_command` helper and added tests that pin the argv and verify shell-quoting of tokens containing spaces and metacharacters.
- Removed the unreachable short-username padding branch in `generate_bot_username` (no input could ever produce a name shorter than 5 characters) and replaced the test that never exercised it.
- Strengthened several weak Telegram tests: per-data-center session encoding is now decoded and checked (IP, port byte, dc byte, key bytes) instead of only the version prefix; the fallback API credentials are pinned to their exact known values; the orchestrator "already in progress" test no longer asserts on the private thread list; the `wait_for_all` test now verifies it returns promptly; and the error-hierarchy tests now exercise real raise/catch behavior.

No user-facing behavior changes.
