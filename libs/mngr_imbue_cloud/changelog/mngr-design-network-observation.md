Add the generation-2 slice backend (specs/slice-fleet-gen2, phase 1): raw qemu VMs under a systemd template unit instead of lima, with routed-tap per-VM networking rendered as nftables DNAT/SNAT rules, per-VM named counters, enforced connection ceilings (50,000 concurrent / 300 new per second), fair-share HTB bandwidth classes, and a guest-to-box management block.

New pure renderers in `slices/qemu_slice.py` (one-time cloud-init material with a stable instance-id, the template unit / root helper / sudoers prep artifacts, and the reserve script with a carve-time real-free-space guard) plus `QemuSliceVpsClient`, a drop-in sibling of the lima client behind the new `SliceVmClientInterface`.

The rendered artifacts confine a VM escape to the slice's own unix user: the sudoers grant is exact per-ordinal command specs (no wildcard), the root helper parses the slice env file with strict per-key validation instead of sourcing it, the slice dir and env stay owned by the service user (the VM's user gets only its disk media and a `run/` dir for its qmp socket and serial log), and qemu runs without the service user's group.

The slice provider selects its backend from the new `box_generation` config value (threaded from the box's DB row by the operator bake); gen-1 boxes keep the lima path unchanged. `SliceProvisionResult` moved to `data_types.py` and gained the gen-2 slice ordinal.
