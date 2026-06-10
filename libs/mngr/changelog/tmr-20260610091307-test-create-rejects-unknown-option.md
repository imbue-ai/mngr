Strengthened the e2e coverage for `mngr create` rejecting an unknown option.

- `test_create_rejects_unknown_option` now also asserts the rejection is a clean usage error rather than an unhandled crash (no Python traceback leaks to stderr).

- Added `test_create_rejects_unknown_option_with_valid_args`, covering the realistic typo scenario where an unknown option appears alongside an otherwise-valid invocation (`mngr create my-task --no-connect --this-flag-does-not-exist`). It verifies the command is still rejected at parse time and that no agent is left behind in `mngr list`.
