Fixed the e2e tutorial connect tests (`test_connect.py`):

- Repaired the e2e fixture, which wrote a duplicate `type = "claude"` key into
  `settings.local.toml` and caused every `mngr create` in the e2e suite to fail
  with a TOML "Cannot overwrite a value" parse error.
- Added `@pytest.mark.timeout(120)` to the interactive `mngr connect` tests, which
  perform a full agent create plus interactive connect and exceed the default 10s
  per-test timeout.
- Added `test_connect_no_start_fails_when_stopped`, covering the documented
  unhappy path for `mngr connect --no-start` (it refuses to connect to a stopped
  agent rather than auto-starting it).
