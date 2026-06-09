Strengthened the unit test suite after a bad-tests audit. No production behavior change; these
changes close gaps where a real regression would have slipped past CI:

- Removed the weak `test_build_pass_env_vars_is_populated` smoke test (it only asserted the result
  was non-empty): `build_pass_env_vars`'s forward-and-drop behavior is already covered meaningfully
  by the existing `..._drops_kitty_terminal_vars` / `..._drops_caller_tmux_session_vars` tests, and
  the per-agent `MNGR_*` drop is trivial set membership not worth a dedicated test.
- Added coverage for the `tool_result` stream-json conversion branch (including the `is_error`
  true/false cases), assistant `tool_use` content blocks, and `_parse_input_preview`
  (empty / valid-JSON / unparseable inputs).
- The user-event stream-json test now asserts the full converted message, not just the envelope type.
- Added raw-transcript coverage for tool-use input-preview rendering and truncation, usage
  conversion (including the empty-usage -> None case), tool_result output truncation, list-form
  tool_result content flattening, and mixed text + tool_result user messages.
- Tightened `test_rejected_flags_raise` to assert the per-flag rejection reason, and added a test
  for the inline `--flag=value` form of pass-through claude value flags.
- Strengthened the `monotonic_ms_since` test to guard the milliseconds scaling, and documented the
  deliberate timing margin in the polling-ticker test.
- Added a lightweight integration test (`test_cli.py`) that drives the real `mngr robinhood`
  command through the top-level CLI for the no-spawn failure paths: it asserts the command is
  registered, that each rejected flag maps to exit code 2, and that a bad `--output-format` value
  exits 2. This is the first end-to-end coverage of the click wiring and exit-code contract.
