Strengthened several slack_exporter tests that previously asserted too weakly to catch the bugs they were named for:

- `test_run_export_recently_active_channels_selects_top_n` now asserts which channels were fetched (the two most recently active), not just how many.
- `test_load_existing_users_updated_overrides_created` now seeds distinguishable user events and asserts the updated value wins (added a `name` parameter to the `make_user_event` test factory).
- Added `test_run_export_detects_relevant_threads_via_mention`, covering the previously untested "mentioned" relevance branch.
- `test_run_export_changed_channels_go_to_updated_stream` now asserts the changed topic value, not just the line count.
- The two `save_*_events_creates_directory_structure` store tests now assert file contents, not just existence.
- Replaced a tautological `"event_id" in model_dump()` assertion in `channels_test.py` with a meaningful check on the generated event ID format.

Structural changes:

- Moved the full-pipeline `test_run_export_*` tests out of the unit-test file `exporter_test.py` into a new integration-test file `test_exporter.py`, leaving the genuinely unit-level `_datetime_to_slack_timestamp` and `_fetch_all_messages_for_channel` tests behind.
- Extracted the CLI's settings-construction and error-to-exit-code logic out of `main()` into the pure, testable helpers `build_settings_from_args` and `run_export_or_exit` (plus a `_build_arg_parser` helper), and added unit tests covering multi-channel space splitting, the `--refresh-window-days 0` -> disabled boundary, env-var cache-TTL parsing, and the exception-to-exit-code mapping.
