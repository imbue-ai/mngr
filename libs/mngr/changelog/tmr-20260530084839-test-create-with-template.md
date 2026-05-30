Fixed the `test_create_with_template` e2e test: removed the superfluous
`@pytest.mark.modal` marker (the test creates a local in-place `command` agent
with `transfer=none` and never invokes Modal) and added `@pytest.mark.timeout(120)`
so the test does not hit the default 10s timeout when, run in isolation, `mngr
create` performs a first-time `ttyd` install on a fresh host. Also strengthened the
verification to confirm the agent's work directory is exactly the session cwd and
added the templates tutorial block for provenance.
