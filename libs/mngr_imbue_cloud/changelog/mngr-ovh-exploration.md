Began the OVH bare-metal "slices" feature: an alternative to ordering OVH VPSes where we carve VPS-like hosts out of bare-metal servers we rent by running lima/QEMU VMs on them.

- Added an OVH catalog pricing helper (`pricing.compute_order_pricing`) that computes the true all-in month-to-month cost of a dedicated-server order: base plan plus every selected add-on delta (RAM/storage/bandwidth), the one-time setup fee, and the first payment. This is the shared, tested source of truth for the bare-metal admin ordering flow, so the catalog's bare "base" price can no longer be mistaken for the real recurring cost.

- Added the slice data model and pure logic (`bare_metal.py`): `BareMetalServer`/`BareMetalServerCapacity` types, `BackendKind`/`BareMetalServerStatus` primitives, and helpers for slot count, slice vCPU sizing with mild CPU overcommit, RAID-level choice, lima naming, slice port allocation, server lifecycle transitions, and server placement.

- Added the lima slice creation path: `build_slice_lima_yaml` produces a VPS-parity lima VM (root SSH, btrfs data disk mounted at the host dir, Docker installed, two external port-forwards for the VM and inner-container sshds), and `LimaSliceVpsClient` provisions/destroys it via limactl. Verified end-to-end against a real lima VM.
