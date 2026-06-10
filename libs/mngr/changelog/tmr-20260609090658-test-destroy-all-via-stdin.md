Fixed the e2e test fixture that wrote a duplicate `type = "claude"` key into
`settings.local.toml`, which made every e2e tutorial command fail to parse its
config. Also corrected the `test_destroy_all_via_stdin` release test: it only
manages local agents, so it never invokes Modal and no longer carries the
`@pytest.mark.modal` mark. Strengthened the test to assert that both agents are
reported as destroyed by the piped `mngr list --ids | mngr destroy - --force`
command.
