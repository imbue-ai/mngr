Introduced a `HostRealizer` seam inside the VPS provider as the first step toward
running agents directly on a cloud VM (no Docker container). The provider now
selects a realizer from a new `isolation` config knob (`IsolationMode.CONTAINER`
| `NONE`); `CONTAINER` is the default and preserves the original behavior
exactly. All Docker-container placement logic (image build/pull, container run,
in-container sshd setup, btrfs volume + snapshot helper, container
stop/start/teardown, and `docker commit` snapshots) moved behind a
`DockerRealizer` that the provider's base methods delegate to. The agent SSH
endpoint, placement lifecycle, and snapshots are now realizer concerns, while the
machine (provisioning, boot, instance lifecycle, host record, discovery) stays
with the provider.

Host-record store resolution also moved behind the realizer
(`realizer.open_host_store(outer, host_id)`), so a non-Docker placement can
persist its host record without a Docker volume. The container realizer
resolves the per-host Docker volume exactly as before.

Added the `BareRealizer`: it places the agent directly on the VM's OS (no
Docker), reached at `vps_ip:22` as root with the same VPS keypair the provider
already uses for the outer. It installs the lightweight host packages and mngr
host_dir layout on the VM (the same setup the container gets, applied to the OS),
keeps the host record in a plain root-disk directory, and reports no snapshot
support. Machine stop/start/destroy stays the substrate's job, so the bare
placement lifecycle steps are no-ops.

Selecting `isolation=NONE` is still accepted by config but currently raises
`BareIsolationNotYetSupportedError`: the realizer is implemented and unit-tested,
but provider-level discovery and listing are still Docker-specific, so bare is
not yet wired end-to-end on aws/gcp/azure. No user-visible behavior changes for
existing aws/gcp/azure/vultr/ovh/imbue_cloud providers.
