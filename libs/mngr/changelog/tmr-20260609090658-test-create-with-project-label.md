Fixed the e2e tutorial test fixture: removed a duplicate `type = "claude"` key
in the generated `settings.local.toml` that produced invalid TOML and broke
`mngr create` across e2e tests. Also added a `@pytest.mark.timeout(120)` mark to
`test_create_with_project_label` so its multi-step `mngr` subprocess calls are
not killed by the global 10s pytest timeout.
