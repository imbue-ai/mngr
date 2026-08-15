Phase 2 of the user-controlled-keys plan: per-host agent client keys and store-routed pinning.

The agent connection's client keys become per-host for new hosts: the container realizer mints a per-host `container_ssh_key` and authorizes only it in the new container, and the bare realizer mints a per-host `vps_ssh_key` and appends it to root's authorized_keys. `agent_endpoint` now takes the host_id it resolves keys for, falling back to the legacy provider-wide pair for hosts created before per-host keys existed.

The provider-wide `vps_ssh_key` deliberately remains the VM-root management key (discovery must reach a VPS before knowing which host lives on it, and OVH's rebuild bootstrap installs it), but it never serves the agent connection for new hosts.

`vps_known_hosts` and `container_known_hosts` are now rendered from mngr core's new host-key pin store: every pin (create, resume rebind, realizers) carries the host_id, `remove_host_from_known_hosts` is store-aware, and destroying a host forgets its pin records alongside its per-host keypairs. The per-host key-dir helpers moved to mngr core, with `mngr_vps.primitives` re-exporting them.
