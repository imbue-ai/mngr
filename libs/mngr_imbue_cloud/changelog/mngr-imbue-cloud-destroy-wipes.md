`mngr destroy <agent>` against an imbue_cloud-leased pool host is now
*terminal* rather than a soft `docker stop`. The new flow on the leased
VPS:

1. Stops + removes the workspace container, drops the per-host docker named
   volume, deletes the per-host btrfs subvolume under `/mngr-btrfs/`, runs
   `docker system prune -a -f --volumes`, and wipes `/root` + `/tmp`
   (preserving only `/root/.ssh/authorized_keys` so the pool-management ssh
   path still works through `cleanup_released_hosts.py`).
2. Releases the lease back to the pool (the `/hosts/{id}/release` connector
   call -- same as `mngr imbue_cloud hosts release`).
3. Cleans up local per-host state (ssh keys, known_hosts, cached records).

Privacy-first ordering: the agent's data is gone before the connector flips
the row to `released`, so the eventual VPS-destroy by
`cleanup_released_hosts.py` is belt-and-suspenders rather than the only
barrier.

To stop the container without releasing the lease (i.e. you intend to
resume the workspace later on the same VPS), use `mngr stop <agent>`
instead.

`mngr delete <agent>` (the GC path) now also runs this same flow; it's a
safe no-op for an already-released lease and acts as a recovery path if a
prior `destroy` crashed mid-wipe.

The wipe script (`build_pool_host_wipe_script`) is exposed as a pure free
function in `mngr_imbue_cloud.instance` so the rendered shell can be unit
tested without standing up an SSH transport.
