Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"`
key into the per-test `settings.local.toml`, producing invalid TOML that made
`mngr create` (and therefore every e2e tutorial test) fail to load its config.
Also gave `test_exec_short_form` an explicit `@pytest.mark.timeout(120)` so the
create+exec cycle is not killed by the repo-wide 10s default timeout.
