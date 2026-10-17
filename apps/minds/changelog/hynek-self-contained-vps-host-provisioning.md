Workspace diagnostics tail a remote workspace's gateway and tunnel logs at their new location, `/var/log/mngr-latchkey/`, where the `mngr-latchkey` package that now provisions the VPS gateway has supervisord write them, and still at `~/.latchkey/` for a host not yet re-provisioned by this build.

The opt-in latchkey remote-workspace e2e test tears the "VPS" down by purging the `mngr-latchkey` package (which stops and unregisters its supervisord programs and drops the RAM-backed secrets and logs) instead of removing the supervisord drop-ins by hand.

The gen-2 cutover runbook no longer describes a latchkey leg of the migrate: the migrate does not carry a workspace's latchkey state, the desktop's next provisioning pass stands the gateway up on the new VM, and the gateway's log is under `/var/log/mngr-latchkey/`.
