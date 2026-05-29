- Hardened the `test_plugin_remove` tutorial e2e test: added a per-test
  `timeout(60)` marker so the slow `mngr` cold start no longer trips the default
  10s timeout, and tightened the assertions to confirm the command fails cleanly
  (no Python traceback) rather than merely returning a non-zero exit code.
