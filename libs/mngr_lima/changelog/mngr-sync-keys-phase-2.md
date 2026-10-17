Phase 2 of the user-controlled-keys plan: per-host root client keys for Lima VMs.

New run-as-root VMs authorize a root client key minted under the host's own keys dir instead of the provider-wide `root_ssh_key`; older VMs keep working via a read fallback to the legacy shared pair. This keeps provider-wide key material out of anything derived per host (e.g. a future synced workspace record).

The per-host known_hosts file is now rendered through mngr core's new host-key pin store via a sole-endpoint pin, preserving the existing behavior that the file reflects only the VM's current forwarded port.
