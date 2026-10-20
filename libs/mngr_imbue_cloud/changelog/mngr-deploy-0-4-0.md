Fixed the slice adoption key reconciler never running at boot: the systemd unit was `WantedBy=multi-user.target` while also `After=cloud-final.service` (which is itself after multi-user.target), an ordering cycle systemd resolved by deleting the reconciler's start job on every boot. Any adopted slice VM that rebooted therefore came back serving its bake-time outer sshd host key, which the owner's client (correctly) refuses -- the workspace read "Status unknown" forever. The unit is now activated by `cloud-init.target`.

- The reconciler install now uses `systemctl reenable` so the stale `multi-user.target.wants` symlink from previously-adopted hosts is dropped when the fixed unit is rolled out.

- The adoption verify/heal pass now compares a content hash of the installed reconciler unit + script against what the current client version ships, so already-adopted hosts get the fixed unit on their next full verification (an `enabled`-but-broken unit previously read as healthy).

- `ADOPTION_SCHEMA_VERSION` is bumped to 2 so every already-adopted host is actually swept through one such full verification: without the bump, hosts verified at version 1 skip the SSH-visiting pass entirely, and the stamp is only otherwise invalidated after a restart -- by which point the broken unit has already left the VM serving its bake-time key, unreachable to the heal.
