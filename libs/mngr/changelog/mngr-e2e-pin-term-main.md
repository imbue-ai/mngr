Pinned `TERM` in the e2e fixture environment, fixing six release tests that failed only in CI.

`mngr connect` replaces itself with `tmux attach`, which needs a terminal type. A developer shell always sets `TERM`; a GitHub Actions runner does not, so the tests passed locally and failed in CI with `open terminal failed: terminal does not support clear`. `watch` fails the same way with `Error opening terminal: unknown.`, and there the surrounding `|| true` turned it into an empty capture that the assertion could not explain.

Fixes all five failing `test_connect` tests (`no_start`, `short_form`, `by_name`, `with_start`, `with_start_restarts_stopped_agent`) plus `test_list_watch_mode` -- eight failure instances across seven shards in release-tests run 35790568923 on main.
