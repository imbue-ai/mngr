Added `just bake-slice-dev` and `just bake-slice-prod` recipes for baking bare-metal slices (lima/QEMU VMs carved on a pre-registered, prepped OVH bare-metal box) into the minds pool.

They are thin wrappers over `minds pool create --backend slice` (which resolves the tier's pool key, and the host_pool DSN for shared tiers, from Vault), mirroring the existing `bake-pool-host-{dev,prod}` recipes for OVH VPSes -- the only difference is the backend. Both require an activated minds env and `vault login` first.
