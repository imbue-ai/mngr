OVH bare-metal slices support:

- Added two `host_pool` migrations: `008_bare_metal_servers.sql` (a new table tracking rented OVH dedicated servers and their resumable lifecycle) and `009_pool_host_slice_columns.sql` (adds `backend_kind`, `bare_metal_server_id`, `lima_instance_name`, and `lima_disk_name` to `pool_hosts` so a pool host can be either a real OVH VPS or a lima-VM "slice"). Existing rows default to `backend_kind = 'ovh_vps'`; leasing is unchanged.

- The release path (`release_host`) and the cleanup sweep now branch on `backend_kind`: a real VPS is still cancelled in OVH, while a slice has its lima VM (and btrfs data disk) destroyed by SSHing the owning bare-metal box and running `limactl`. A slice whose VM cannot be destroyed keeps its row in `removing` so the slot is only freed once the VM is really gone.
