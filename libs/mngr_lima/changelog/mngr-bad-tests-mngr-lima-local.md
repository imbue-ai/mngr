Strengthened several low-value Lima provider unit tests so they actually verify
behavior:

- `LimaHostStore` cache test now mutates the on-disk record behind the cache and
  asserts that a default read serves the stale value while a cache-bypass or
  `clear_cache()` reflects the new disk state (previously it could not tell a
  working cache from a no-op one).
- Replaced the tautological `LimaSshConfig` constructor test with tests for the
  `limactl show-ssh` output parser; extracted the parsing loop into a pure
  `_parse_show_ssh_output` helper to make it unit-testable.
- `LimaProviderConfig` custom-config test now asserts a JSON round trip
  (verifying tuple/Path coercions) instead of echoing constructor arguments.
- `reset_caches` test now asserts the caches are actually emptied; the provider
  directory test pins the exact path; `get_volume_for_host` test asserts the
  returned volume is rooted at the host's volume directory.
- Default Lima YAML test asserts the arch-appropriate default image URL, and the
  temp-file writer test verifies serialized content and cleans up via
  `try/finally`.

No user-facing behavior changes; `_parse_show_ssh_output` is a pure refactor of
existing parsing logic.
