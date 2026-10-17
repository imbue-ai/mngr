Phase 2 of the user-controlled-keys plan: a structured host-key pin store in mngr core.

New `imbue/mngr/providers/host_key_store.py`: every provider known_hosts file gains a JSON pin-store sidecar and becomes a derived artifact rendered from it. Pins carry an origin (BOOTSTRAP or USER) with the precedence rule that a user-origin pin is displaced only by newer user material while bootstrap-origin pins are replaceable by anything, and pinning always replaces per (endpoint, keytype). Out-of-band lines written to a known_hosts file are imported on the next store operation, so unmigrated writers lose nothing.

`add_host_to_known_hosts` / `clear_host_from_known_hosts` keep their signatures but write through the store when given a `host_id` or when a store already exists for the file; host_id-less callers on store-less files keep the legacy direct-write behavior (throwaway known_hosts files stay sidecar-free).

The docker provider mints a per-host client key (`docker_ssh_key`) and a per-host container sshd host key for every new container, resolving per-host first with a legacy shared-pair fallback for older containers; `delete_host` forgets the host's pins and per-host keypairs (dead-endpoint GC). Shared per-host keypair helpers (`per_host_key_dir`, load-or-create and resolve-with-fallback variants) are now in `providers/ssh_utils.py` for all providers.

Each per-host key dir also carries a `known_hosts` symlink to the provider-wide rendered file, so consumers that derive the pinned-host-keys file as the private key's sibling (the forward SSH tunnel) keep working with per-host client keys.
