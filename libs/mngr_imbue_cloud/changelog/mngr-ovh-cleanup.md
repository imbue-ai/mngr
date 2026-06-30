Removed the legacy OVH-VPS pool-host backend from `mngr imbue_cloud admin pool`. Pool hosts are now exclusively bare-metal slices (lima VMs carved on our bare-metal boxes).

- `admin pool create` is slice-only: the `--backend` flag and the OVH-VPS-only flags (`--tag`, `--management-public-key-file`, `--no-recycle`) are gone, and `--server-id` (the bare-metal box to bake onto) is required.

- `admin pool destroy` always tears down the slice's lima VM before dropping the row (the OVH-VPS cancel path is removed); `--skip-vps-cancel` still skips teardown when the VM is already gone.

- Dropped the `backend_kind` discriminator (CLI/value/column) — there is only one backend now.

OVH as the bare-metal-box supplier is unchanged: box ordering, OVH catalog pricing, region/datacenter validation, and the `bare_metal_servers` records all remain.
