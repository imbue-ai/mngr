Added a `mngr forward --on-error {abort,continue}` flag (default `abort`). Under
`continue`, the `--no-observe` startup snapshot tolerates an
unauthenticated/unreachable provider: it runs `mngr list --on-error continue` and
forwards the agents the healthy providers reported instead of failing to start.
The flag affects only `--no-observe`; the observe and `--observe-via-file` modes
already tolerate provider errors and are unchanged.
