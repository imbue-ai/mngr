Strengthened the mngr-pair test suite (test-only changes; no user-facing
behavior changed):

- The `pair` CLI tests now assert on specific error messages and the exact set
  of choice values rather than near-tautological substrings, the
  `--source`/`--source-agent` conflict test now actually triggers and verifies
  the conflict, and the structured (JSONL/JSON) output of pair start/stop events
  is checked.
- The `UnisonSyncer` command-building tests assert the exact command and ignore
  pairs instead of loose substring membership.
- The crash-simulation test no longer uses a broad `pkill -f`; it locates the
  specific unison PID with `pgrep` and kills that exact PID. The two dependency
  tests use real PATH manipulation instead of faking `shutil.which`. The
  symlink-sync test waits for both the symlink and its target, removing a race
  (and its `flaky` marker).
