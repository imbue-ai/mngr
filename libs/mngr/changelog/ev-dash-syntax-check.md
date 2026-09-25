Fixed the `_build_remote_lock_command` tests so their intended shell syntax check no longer executes the command.

The tests validated the generated lock command with `sh -n -c <cmd>`, but dash (the default `/bin/sh` on Debian/Ubuntu) ignores `-n` for `-c` scripts and actually runs them, so the command's `mkdir -p` executed and failed on an unprivileged or read-only `/`. The script is now fed via stdin (`sh -n` with `input=cmd`), where dash honors `-n` and performs a true no-exec syntax check.
