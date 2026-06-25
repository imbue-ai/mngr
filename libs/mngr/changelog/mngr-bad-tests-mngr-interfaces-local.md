Test-quality cleanup under `imbue/mngr/interfaces` (no production-code changes):

- Replaced tautological "constructor echo" unit tests (which only re-asserted the
  values just passed in) with behavioral assertions: `SSHInfo` and `HostDetails`
  now have JSON round-trip tests that exercise the real `Path`->str and enum->value
  coercions, and `VolumeFile` is now checked via the `listdir` logic that derives
  its `size` from actual file contents.
- Tightened a loose `len(data) > 0` assertion in the scoped-volume listdir test to
  assert exact file contents, so it can no longer pass if a scoped read resolved to
  the wrong backing file.
- Moved the in-memory `Volume` test double out of `volume_test.py` into a shared
  `interfaces/mock_volume_test.py` (per the "mock implementations live in
  `mock_*_test.py`" convention) and gave its `files` field a proper
  `default_factory`.
- Removed all `unittest.mock` usage from `provider_instance_test.py`: the host/agent
  fallback, disconnect, and resilience tests now use concrete typed mock
  implementations (`MockOnlineHost`, `MockAgent`) instead of `MagicMock`, asserting
  observable effects (e.g. a `disconnect_count` counter, offline-derived agent
  state) rather than coupling to call counts. The `unittest.mock` ratchet is now 0.
