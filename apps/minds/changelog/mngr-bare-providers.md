Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.
