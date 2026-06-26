Add a `just minds-test-electron-flow` recipe that drives the full minds Electron workspace lifecycle end-to-end under `xvfb` (create a local Docker workspace -> send a chat message and await the agent's reply -> open a terminal -> navigate home -> destroy via the v1 settings flow), complementing the create-only `just minds-test-electron` acceptance test.

Also fix `just sync-vendor-mngr` to resolve the forever-claude-template path to an absolute path before use, so passing a relative path no longer breaks the recipe's second `cd`.
