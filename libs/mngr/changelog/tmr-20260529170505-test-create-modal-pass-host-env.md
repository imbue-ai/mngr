Fixed the `test_create_modal_pass_host_env` e2e release test so it runs against the
current `mngr create` behavior. The test now passes an explicit `--type command`
(running `sleep`) instead of relying on a default agent type, which is no longer
hardcoded. The test also now verifies, via `mngr exec`, that the value forwarded
through `--pass-host-env` actually reaches the remote Modal host.
