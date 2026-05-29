Strengthened the `mngr config set headless` e2e tutorial test: it now verifies
the value is actually persisted to the project `settings.toml` (as a real
boolean, not the string `"true"`) instead of only checking the command's exit
code. Added a companion unhappy-path test confirming that `mngr config set`
rejects an unknown configuration key and does not persist the rejected write.
