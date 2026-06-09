Test-only changes (no user-visible behavior change):

- Fixed the e2e tutorial test fixture (`e2e/conftest.py`): the generated
  `settings.local.toml` had a duplicate `type = "claude"` key under
  `[commands.create]`, which made TOML parsing fail with "Cannot overwrite a
  value". This broke agent creation for every e2e tutorial test. Removed the
  redundant key.
- Cleaned up `e2e/tutorial/test_event.py`: removed a duplicate
  `_parse_jsonl_events` definition that shadowed the stricter one, and
  strengthened `test_event_head` to assert that `--head` returns the leading
  prefix of the full event stream (the earliest events, not the tail).
