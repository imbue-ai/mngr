Fixed the `test_create_with_extra_tmux_windows` e2e release test: it now overrides
the default 10s function timeout (a real local create plus `mngr list` can exceed
it, especially when `ttyd` is being installed) and no longer carries a stale
`@pytest.mark.modal`. Since `mngr list` stopped auto-creating the per-user Modal
environment for read-only commands, this purely local-provider test never invokes
Modal, so the resource guard was correctly flagging the mark as never-invoked.
The test also now exercises two named extra tmux windows (`server` and `logs`),
matching the tutorial more faithfully.
