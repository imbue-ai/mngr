Fixed the `test_gc_default` release test (tutorial GC block). The `mngr gc`
verification step now uses a 90s timeout (Modal provider discovery is a slow
network round-trip that exceeded the default 30s), and the post-gc survival
check scopes `mngr list` to `--provider modal` so it no longer fails when other
providers (e.g. AWS) are unconfigured in the test environment.
