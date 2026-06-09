Fixed the e2e test fixture's `settings.local.toml` generation, which wrote a
duplicate `type = "claude"` key under `[commands.create]` and caused every e2e
`mngr create` to fail with a TOML "Cannot overwrite a value" parse error.

Strengthened `test_create_with_message` to verify the initial message is
actually delivered into the agent's tmux pane (via `tmux capture-pane`), rather
than only checking that mngr logged "Sending initial message".
