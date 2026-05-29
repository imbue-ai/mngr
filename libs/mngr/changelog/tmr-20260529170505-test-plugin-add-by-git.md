Hardened the `test_plugin_add_by_git` tutorial e2e test: raised its pytest
timeout to 60s so the ~8-9s mngr CLI startup no longer trips the global 10s
timeout under load, and strengthened its assertions to confirm the failed
install reports a clean error (no traceback) and leaves no `mngr-plugin`
registered in `mngr plugin list`.
