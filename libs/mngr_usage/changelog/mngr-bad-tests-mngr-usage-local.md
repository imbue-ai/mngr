Test-quality improvements in `mngr_usage` (no user-visible behavior change):

- Tightened the `mngr usage wait` CLI tests: assert the exact "Matched on source
  'claude'" success line and the exact "Invalid include filter expression" error, and
  require the no-agents subcommand-flag case to time out (exit 2) rather than accepting
  either exit code.
- Added coverage for previously-untested surfaces: the `_format_human_line` "reset time
  unknown" branch, the `mngr usage wait --format json` result payload, the first-of-many
  match ordering in `wait_for_usage`, and a parity check that the wait CEL context mirrors
  the `--format json --detail` per-source keys.
- Isolated the no-timestamp drop case in the aggregation test, and parametrized the
  duration/reset-phrase formatting tests.
