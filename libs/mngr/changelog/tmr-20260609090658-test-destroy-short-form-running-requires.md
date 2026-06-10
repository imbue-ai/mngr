Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` that made TOML parsing
fail ("Cannot overwrite a value") and broke `mngr create` in every tutorial e2e
test. Also removed an incorrect `@pytest.mark.modal` from
`test_destroy_short_form_running_requires_force`: that unhappy-path test refuses
to destroy a running local agent without `--force`, so it never invokes modal.
