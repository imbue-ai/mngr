Fixed the `test_gc_provider_modal` e2e tutorial test so its post-gc verification scopes the listing to the modal provider (`mngr list --provider modal --format json`).

Previously the test ran an unscoped `mngr list --format json`, which discovers every enabled backend. In an environment where a backend is enabled but unconfigured (e.g. AWS with no credentials), that provider raises `ProviderUnavailableError`, which aborts the whole list under the default `--on-error abort` and made the test fail with exit code 1. Scoping the verification to the modal provider mirrors the `mngr gc --provider modal` command under test and keeps the check focused on the relevant provider, where the agent actually lives.

Also strengthened the verification: instead of only checking that the agent name appears in the listing, the test now parses the JSON and asserts the agent's Modal host is still `RUNNING` after gc, directly verifying the gc invariant that an active machine must not be torn down.
