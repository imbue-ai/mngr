Added an opt-in `is_host_in_docker` mode to the Lima provider
(`providers.lima.is_host_in_docker`, default `false`). When enabled, the agent
runs inside a Docker container *in* the Lima VM (built from the project's
Dockerfile, exactly like the docker/vps_docker providers) instead of directly
in the VM. mngr treats the container as the host: ssh and all agent work happen
inside it, and Lima forwards the container's sshd out to the host's localhost.

The mode forces the in-VM btrfs additional-disk layout
(`is_host_data_volume_exposed` must be `false`): a per-host btrfs subvolume on
that disk backs the container's `host_dir`, and the `mngr_vps_docker` snapshot
helper is installed in the VM so the in-container agent can trigger consistent
`btrfs subvolume snapshot` backups (same `/mngr-snapshot` / `/mngr-snapshots`
contract as the other docker providers). `mngr stop` powers off the whole VM;
`start` boots it and relaunches the container; `destroy` removes the VM and the
disk. Default (`is_host_in_docker=false`) behavior is unchanged.
