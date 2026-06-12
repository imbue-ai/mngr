Fixed `builder = "DEPOT"` builds, which were broken for all VPS backends (aws/vultr/ovh).
The depot CLI installs to `$HOME/.depot/bin/depot`, which is not on the non-interactive
shell's PATH, but `build_image_on_outer` invoked it by bare name (`depot build ...`),
failing with `bash: line 1: depot: command not found`. The CLI is now resolved at run
time: a `depot` already on PATH is preferred (so an existing install is respected),
otherwise it falls back to the installer's off-PATH default `$HOME/.depot/bin/depot`,
installing there only when nothing is found. The same resolved path drives both the
idempotent install check and the `depot build` invocation.

A second bug in the same path also blocked depot: `DEPOT_TOKEN` was forwarded via the
streaming SSH command's `env`, but env forwarding for compound commands was broken in
`mngr` core (see the `mngr` changelog) so the token never reached `depot build`
("missing API token"). Both are now fixed.
