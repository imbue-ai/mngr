The imbue_cloud slow (rebuild) path now re-applies the full idempotent host
setup on the leased VPS before rebuilding the container: it ensures the pinned
Docker version, installs/registers gVisor `runsc` if missing, tunes sshd, and
installs the base packages. This runs after the old container is torn down and
before the rebuild, and a failure is fatal. The result is that a workspace
created via the slow path -- even against a host baked with an old version, or
one baked before runsc existed -- comes up consistent and runs its agent
container under gVisor.

`ImbueCloudProviderConfig` now extends `VpsDockerProviderConfig`, so it carries
`docker_runtime` / `install_gvisor_runtime` / `default_start_args`; the delegated
vps_docker provider used for the rebuild forwards these, so the rebuilt container
runs under `--runtime runsc` with the `--workdir=/` and
`--security-opt=no-new-privileges` hardening args. These values are written into
the per-account `[providers.imbue_cloud_<slug>]` block by minds bootstrap.
