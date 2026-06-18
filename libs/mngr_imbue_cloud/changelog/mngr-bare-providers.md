Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

Updated the slice provider's `_create_host_object` / `_wait_for_container_sshd` /
`_on_certified_host_data_updated` overrides to match the base's new per-host
realizer threading (the base now resolves an existing host's realizer from its
placement rather than the create-time config). imbue_cloud is container-only, so
it threads the realizer through unchanged; no behavior difference.
