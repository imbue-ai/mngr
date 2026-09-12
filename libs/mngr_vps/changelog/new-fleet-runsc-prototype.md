The pinned gVisor download + sha512 verification is now the public `PINNED_GVISOR_BINARY_INSTALL_SCRIPT` (with `GVISOR_RUNSC_RUNTIME_ARGS` / `GVISOR_RUNSC_BINARY_PATH`), so the gen-2 slice guest image customization in `minds_admin` bakes the identical pinned binaries into the image; the live-host `runsc install` step is composed from it and is unchanged in behavior.

- `ensure_btrfs_subvolume_on_outer` joins the new subvolume to the host quota group (`HOST_QUOTA_QGROUP`, `1/0`) when the data filesystem has one (gen-2 slices); filesystems without quotas are unchanged.
