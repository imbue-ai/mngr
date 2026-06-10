Fixed the e2e tutorial test fixture so `mngr event` tutorial tests can create agents again.

- The e2e fixture wrote a duplicate `type = "claude"` key inside `[commands.create]` in the
  generated `settings.local.toml`, producing invalid TOML. Every tutorial command that creates
  an agent failed with "Cannot overwrite a value". Removed the duplicate key.
- Test-only cleanup in `test_event.py`: removed a duplicated `_parse_jsonl_events` helper (the
  second, weaker definition shadowed the first) and strengthened
  `test_event_head_conflicts_with_tail` to assert that no events are emitted to stdout when
  `--head` and `--tail` are combined.
