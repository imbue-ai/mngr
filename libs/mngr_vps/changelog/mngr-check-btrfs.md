- The outer-VM btrfs snapshot helper no longer touches the data volume before
  it is actually mounted: `handle_request` now defers (polls `mountpoint`
  every 5s with a periodic journal warning, never fails) until
  `MNGR_BTRFS_MOUNT_PATH` resolves to a mounted volume. Previously, on slice
  VMs -- where lima mounts the data disk at a highly variable point late in
  boot, well after the helper's `local-fs.target` ordering -- the helper's
  startup replay of a `request.json` left over from before a reboot ran
  pre-mount: its `mkdir -p` wrote a shadow `snapshots/` directory onto the
  root fs beneath the unmounted mountpoint (debris that defeated the
  workspace autostart trigger during the mngr-internal#266 follow-up testing,
  see default-workspace-template#381), and the request was failed -- then
  permanently retired by the result-id idempotency guard -- when it only
  needed to wait a few seconds for the mount. A deferred request is now
  serviced normally once the volume appears, within the inner requester's own
  result timeout.

- `prepare_btrfs_on_outer` now refuses (raises `VpsProvisioningError`) when
  the btrfs mount path is a symlink -- the pre-mounted slice layout -- but
  nothing is mounted at its target yet. Previously the slice detection was a
  pure presence gate, so running before the VM's data-disk provisioning
  finished would silently fall through to building a btrfs loop file on the
  VM's root disk: a wrong-but-working state that masks the real volume (and
  its content) from then on.
