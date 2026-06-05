Fixed the create-template e2e tutorial tests (`test_templates.py`). The
`test_templates_setup_via_config_edit` test now opts the project `settings.toml`
that `mngr config edit --scope project` creates into the pytest run (via
`is_allowed_in_pytest = true`) before invoking `mngr create`, so the config
loader no longer refuses the freshly created project config. Also added the
missing `@pytest.mark.tmux` to all three template tests, which create local
command agents that use tmux.
