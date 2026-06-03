Refactored `VpsDockerProvider.create_host` so the post-ordering work (container
build/run, SSH setup, certified-data + host-record finalize) lives in a single
public method, `create_host_on_existing_vps`, that operates over a caller-supplied
outer SSH connection and makes no VPS-API (ordering) calls. `create_host` now
orders the VPS and then calls it, so there is exactly one "set up the host after
the VPS exists" code path.

Added `teardown_container_on_existing_vps` to remove a host's container + per-host
btrfs subvolume + named volumes on an already-reachable VPS (no VPS-API calls),
for rebuilding a container in place.

Added `ExternallyManagedVpsClient`, a `VpsClientInterface` stub for providers that
operate on a VPS they did not order (e.g. an imbue_cloud-leased pool host); every
ordering/snapshot/ssh-key call raises so a wrong call site fails loudly.

These are consumed by `mngr_imbue_cloud`'s new slow path; existing OVH/Vultr
behavior is unchanged.
