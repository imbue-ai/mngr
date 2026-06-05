# Test-quality hardening across the `api` layer

Reviewed every test file under `imbue/mngr/api` and fixed tests that could pass on
a real bug or fail for the wrong reason. This is a test-only change with no
user-facing behavior difference; it makes the suite catch regressions it
previously missed.

Highlights:

- Rewrote tautological hook tests to drive the real destroy path (`execute_cleanup`)
  so hook firing/order is verified as a production side effect rather than self-fired.
- Rewrote the host-name conflict test to actually exercise the retry loop in
  `resolve_target_host`, and added a test that user-specified names are not retried.
- Replaced loose substring / `>=` / `is not None` / "didn't raise" assertions with
  exact content, counts, and exclusion proofs across `gc`, `events`, `list`, `find`,
  `discovery_events`, and `message` tests.
- Removed a global `monkeypatch` of `BaseAgent.send_message` in favor of an injected
  failing agent (clears a ratchet violation without evasion).
- Added remote (`is_local=False`) coverage for git URL/env and rsync SSH-transport
  assembly, which no happy-path test previously exercised.
- Replaced `int(time.time())` agent/session names with `uuid4` to avoid collisions
  under xdist, and replaced multi-day `sleep` magic numbers with a small constant to
  cap leaked-process lifetime on a missed cleanup.
- Added `try/finally` teardown around tmux agents so a failing assertion no longer
  leaks a real process and tmux session.
- Deleted tautological Pydantic round-trip tests (`MessageResult`, `ExecResult`,
  `RsyncResult`, `ListResult`, `AgentMatch`) that only re-tested the model layer.
- Hardened the SIGWINCH delivery flaky test's catcher to re-arm in a loop (markers
  kept on the genuinely flaky tests per convention).
