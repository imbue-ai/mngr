The `-b generation=<n>` build argument is removed (mngr-internal `new-fleet-phase-2`, slice-fleet cutover phase 2): the connector no longer filters leases by box generation, so `-b region=` is the only imbue_cloud placement knob.

- The rebuild provider caps the workspace container's memory from the lease's `memory_units` alone; the `attributes.memory_gb` fallback is gone. A lease that carries no machine size (an older connector) logs a warning and rebuilds without a memory cap (`CLEANUP`-marked until every tier's connector serves the sizing columns).

- `compute_gen1_migrated_data_disk_gib`: the size a gen-1 machine's data disk has after the gen-2 cutover (the gen-1 disk plus the 16 GiB gen-2 data-disk base), shared by the connector's migration 039 backfill and the gen-1 bake's row stamping.

- `slices/bare_metal.py` gains `CI_SLICE_MAX_AGE_SECONDS` (4 h, beside the orphan reaper's `ORPHAN_SLICE_MIN_AGE_SECONDS`) and the tier-scoped classifiers `partition_slice_names_by_tier_and_age` / `compute_tier_orphan_disk_names`, the shared "old enough to be abandoned" logic the operator CLI's CI slice sweep runs on either box generation.
