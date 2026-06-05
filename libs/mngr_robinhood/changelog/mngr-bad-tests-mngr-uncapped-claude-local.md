Strengthened the unit test suite after a bad-tests audit. No production behavior change; these
changes close gaps where a real regression would have slipped past CI:

- `build_pass_env_vars` is now tested for its actual purpose -- forwarding the parent environment
  while dropping the per-agent `MNGR_*` / `LLM_USER_PATH` vars -- instead of merely asserting the
  result is non-empty.
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
