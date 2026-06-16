Hardened the `test_create_rejects_malformed_label` e2e tutorial test so its "no agent was created" verification scopes `mngr list` to the local provider (`--provider local`). The create under test targets the local host, and scoping keeps the listing from reaching out to (and hard-failing on) optional cloud-provider plugins that are installed but unconfigured in the monorepo test environment.

Added a parallel unhappy-path test, `test_create_rejects_malformed_host_label`, covering a malformed `--host-label` from the same tutorial block.
