Fixed the `mngr list --host-label` e2e tutorial test so it no longer carries a
`@pytest.mark.modal` mark it cannot satisfy: `mngr list` on a fresh environment
skips the Modal provider (the environment does not exist yet) and never runs the
`modal` binary, so the resource guard correctly reported the mark as superfluous.
Also strengthened the test to verify the empty-result output and added an
error-path test for an invalid `--host-label` value (missing `KEY=VALUE`).
