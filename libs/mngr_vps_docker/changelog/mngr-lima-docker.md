Extracted the reusable docker/btrfs/snapshot-helper/image-build helpers out of
`VpsDockerProvider` into a new `imbue.mngr_vps_docker.container_setup` module
with public names (e.g. `run_container`, `provision_snapshot_helper_on_outer`,
`prepare_btrfs_on_outer`, `setup_container_ssh`,
`build_image_on_outer_from_build_args`). `VpsDockerProvider` now imports them,
and the `_setup_container_ssh` / `_build_image_on_vps` methods delegate to the
shared functions. No behavior change for VPS Docker hosts; this is the shared
toolkit the Lima provider's new docker-in-VM mode builds on.
