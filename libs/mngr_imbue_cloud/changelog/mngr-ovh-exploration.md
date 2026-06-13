Added the OVH bare-metal "slices" feature: an alternative to ordering OVH VPSes where we carve VPS-like hosts out of bare-metal servers we rent by running lima/QEMU VMs on them. A slice is indistinguishable from a baked VPS pool host to minds and the imbue_cloud provider, but with cleaner btrfs (the lima data disk, no loopback).

- OVH order pricing helper (`pricing.compute_order_pricing`): true all-in month-to-month cost (base plan + every selected add-on delta + one-time setup + first payment), so the catalog's bare "base" price can't be mistaken for the real cost.

- Slice data model + pure logic (`bare_metal.py`): `BareMetalServer`/`BareMetalServerCapacity` types, `BackendKind`/`BareMetalServerStatus` primitives, and helpers for slot count, slice vCPU sizing with mild CPU overcommit, RAID-level choice, lima naming, slice port allocation, server lifecycle transitions, and "most-free ready server" placement.

- Lima slice creation: `build_slice_lima_yaml` produces a VPS-parity lima VM (root SSH, btrfs data disk at the host dir, Docker, two external port-forwards for the VM and inner-container sshds), and `LimaSliceVpsClient` provisions/destroys it via limactl. `SliceVpsDockerProvider` runs the shared vps_docker container bake on the VM (overriding only the per-host-port + btrfs-subvolume seams), producing a baked, reachable host. Verified end-to-end against a real lima VM.

- Admin CLI (`mngr imbue_cloud admin server`): `list` (per-server + fleet slot accounting), `register` (record a delivered box), `allocate-slice` (placement + the slice's lease attributes), `set-status` (advance the resumable order->delivered->installing->ready lifecycle), backed by a Neon access layer (`bare_metal_db`) that writes `bare_metal_servers` + slice `pool_hosts` rows directly.
