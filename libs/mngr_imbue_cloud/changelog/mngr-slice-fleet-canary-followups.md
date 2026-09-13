`mngr create` against imbue_cloud gains `-b generation=<n>`: a hard slice-fleet generation requirement for the lease (validated against the known generations, threaded through both create paths like `region`, never relaxed). For targeting one generation during mixed-generation fleet windows (turnover, the CI split fleet).

`BareMetalServer`'s WireGuard fields are spelled out (`wireguard_address` / `wireguard_public_key`), and `PoolHostDestroyTarget` now carries the box's overlay address so operator teardowns can dial a locked-down box over the WireGuard overlay.

Slice VM clients accept a `box_ssh_port` (management SSH may now be a tunnel's local forward rather than `:22`), and the slice provider config splits `box_management_address` / `box_management_ssh_port` (the carve's management dial) from `box_public_address` (the slices' forwarded ports and the pool row's user-facing address).
