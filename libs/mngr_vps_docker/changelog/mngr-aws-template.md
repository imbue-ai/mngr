Fixed `builder = "DEPOT"` builds, which were broken for all VPS backends (aws/vultr/ovh).
The depot CLI installs to `$HOME/.depot/bin/depot`, which is not on the non-interactive
shell's PATH, but `build_image_on_outer` invoked it by bare name (`depot build ...`),
failing with `bash: line 1: depot: command not found`. It is now invoked by absolute path
(double-quoted `"$HOME/.depot/bin/depot"` so the remote shell expands `$HOME`), and the
idempotent install check tests for that same binary (`test -x "$HOME/.depot/bin/depot"`)
instead of `command -v depot` (which never sees the off-PATH binary and so re-ran the
installer on every build).

A second bug in the same path also blocked depot: `DEPOT_TOKEN` was forwarded via the
streaming SSH command's `env`, but env forwarding for compound commands was broken in
`mngr` core (see the `mngr` changelog) so the token never reached `depot build`
("missing API token"). Both are now fixed.
