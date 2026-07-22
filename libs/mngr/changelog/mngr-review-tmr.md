`mngr tmr` gained a `--reducer-env` option, documented in the generated CLI reference. It passes environment variables to the reducer agent only, never to the mappers, for credentials the reducer needs but mappers must not receive (such as a token that can push and open pull requests).

The `mngr tmr` help text now describes the reducer's full role (collapsing repeated changes, writing the run's changelog, opening the pull request), the explicit per-test `--timeout` its agents run with, and the `escalations` field in the outcome schema.
