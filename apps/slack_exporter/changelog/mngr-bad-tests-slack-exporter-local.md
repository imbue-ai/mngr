Strengthened several slack_exporter tests that previously asserted too weakly to catch the bugs they were named for:

- `test_run_export_recently_active_channels_selects_top_n` now asserts which channels were fetched (the two most recently active), not just how many.
- `test_load_existing_users_updated_overrides_created` now seeds distinguishable user events and asserts the updated value wins (added a `name` parameter to the `make_user_event` test factory).
- Added `test_run_export_detects_relevant_threads_via_mention`, covering the previously untested "mentioned" relevance branch.
- `test_run_export_changed_channels_go_to_updated_stream` now asserts the changed topic value, not just the line count.
- The two `save_*_events_creates_directory_structure` store tests now assert file contents, not just existence.
- Replaced a tautological `"event_id" in model_dump()` assertion in `channels_test.py` with a meaningful check on the generated event ID format.
