Fixed `builder = "DEPOT"` builds, which were broken for all VPS backends (aws/vultr/ovh).
The depot CLI installs to `/root/.depot/bin/depot`, which is not on the non-interactive
shell's PATH, but `build_image_on_outer` invoked it by bare name (`depot build ...`),
failing with `bash: line 1: depot: command not found`. It is now invoked by absolute path
(`/root/.depot/bin/depot`), and the idempotent install check tests for that same binary
(`test -x /root/.depot/bin/depot`) instead of `command -v depot` (which never sees the
off-PATH binary and so re-ran the installer on every build).
