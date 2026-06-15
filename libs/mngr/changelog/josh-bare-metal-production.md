Regenerated the bundled CLI reference docs to include the new `mngr imbue_cloud admin server pricing` command (per-slice OVH bare-metal pricing table).

Regenerated the `imbue_cloud` CLI reference docs to include the new `admin server` command group (list / register / allocate-slice / set-status) added for the OVH bare-metal slices feature.

- `mngr create --format json` (and `--format jsonl`) now also reports the created host's name and SSH connection (`ssh_user` / `ssh_host` / `ssh_port`), plus an `outer_ssh_port` when the provider exposes a separate outer/management sshd (e.g. an OVH-slice's VM-root port reached via a box-forwarded port). Previously only `agent_id` / `host_id` were emitted. A new `HostInterface.get_outer_ssh_port` hook (default `None`) backs this.

- `VpsDockerProvider.record_outer_host_key` pins an outer (VPS-root) sshd host key in the provider's known_hosts -- used when operating on a VPS the provider did not order itself (e.g. the imbue_cloud rebuild on a leased host) so its outer connections pass strict host-key checking.

- `mngr create --format json` now also reports the agent SSH endpoint's on-disk private key path (`ssh_key_path`), so pool-bake tooling can run post-bake SSH steps against the host without a second `mngr list` round-trip.
