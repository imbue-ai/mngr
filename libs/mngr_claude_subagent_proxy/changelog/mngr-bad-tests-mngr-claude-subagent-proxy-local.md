Strengthened several weak tests in the subagent-proxy test suite (no user-facing behavior change):

- Marked the known-failing `test_plan_mode_propagates_to_subagent` release test `xfail(strict=True)` so it stops showing as an unexplained red and will flip to a real regression test the day plan-mode propagation is implemented.
- Added execution-based tests for the generated wait-script (run under bash with a stub `uv`) covering the idempotent short-circuit, EXIT-trap secret cleanup on `mngr create` failure, the happy-path relay, and `--spawn-only` mode, plus a `bash -n` syntax check -- the prior tests only string-matched the generated shell source.
- Factored the target-presence rate-limit decision into `_should_recheck_target_presence` and tested its cadence directly, replacing a test that only compared two module constants.
- Added positive provisioning assertions (settings.local.json hooks actually written) and no-op side-effect assertions where they were missing, and broadened `extract_assistant_text` edge-case coverage.
