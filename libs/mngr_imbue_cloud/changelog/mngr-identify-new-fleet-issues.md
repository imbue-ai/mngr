Pre-cutover fleet fixes (`blueprint/pre-cutover-fleet-fixes/`).

- The gen-1 slice boot disk is 32 GiB again (`SLICE_BOOT_DISK_GIB`), matching `main` and every deployed gen-1 slice; cutover phase 1 had lowered the shared constant to 20 before gen-2 got its own 10 GiB constant.

- The gen-2 slice helper sizes the HTB root and default classes at 95% of the box's declared uplink (`GEN2_UPLINK_SHAPING_PERCENT`), and every machine's guarantee and ceiling derive from that shaped rate, so the shaper rather than the NIC queue is the bottleneck.

- `BareMetalServer.uplink_mbps` is required (the column is NOT NULL after connector migration 040).

- The lease-region label to OVH datacenter pairing (`OVH_DATACENTER_CODE_BY_US_REGION`) now lives in `slices/gen2_scripts/regions.py`, the leaf the connector container mounts; `primitives` re-exports it. New `assert_region_label_matches_box_datacenter` refuses a bake whose label does not name the target box's datacenter.

- `draining` is documented as the permanent box-maintenance status rather than a turnover-only one.
