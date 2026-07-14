Raised the per-test timeout on the `mngr config path` e2e tutorial test to 60s, matching the other multi-subprocess config tests, so it no longer fails against the default 10s pytest-timeout.
