Region-aware leasing for imbue_cloud hosts.

- `mngr create` against imbue_cloud now accepts two new build-arg knobs: a hard `-b region=<datacenter>` requirement (only a host in that datacenter is leased, else the create fails) and a soft `-b preferred_region=<datacenter>` preference (a host in that datacenter is preferred, but any available host is still returned so the fast path is never blocked). Both are validated against the known OVH-US datacenters (`US-EAST-VA`, `US-WEST-OR`); an unknown value fails fast.
- Both knobs are sent to the connector's lease endpoint as separate fields (not folded into the JSONB attribute filter) and are applied on both the fast (adopt) and slow (rebuild) create paths. A hard `region` is preserved through the slow path's attribute relaxation.
- `mngr imbue_cloud admin pool add` now records the bake `--region` (OVH datacenter) into the new `pool_hosts.region` column so the connector can filter/order on it.
