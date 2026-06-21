Multiple developer environments can now safely share a single bare-metal slice box.

Each slice's lima instance and data-disk names are now stamped with the owning environment (`mngr-slice-<env>-<host-hex>`); `admin pool create --backend slice` takes a new `--slice-env-name` for this. Legacy un-stamped slices keep working and are never touched.

Slice baking now derives free-slot capacity from the box's real occupancy (every env's slices plus any legacy ones) instead of the per-env database, so independent envs cannot collectively over-subscribe a box.

Each slice carve now reserves its slot and host ports under a brief box-wide lock (it creates the instance without booting via `limactl create`, then boots it after releasing the lock), so concurrent bakes from different envs never collide on capacity or ports.

The post-bake orphan reaper now only ever deletes the active env's own stamped slices -- never another env's slices or legacy un-stamped ones.

Added `admin pool teardown-slices`, which tears down every unleased slice VM recorded in the pool DB (used by `minds env destroy` so a destroyed env doesn't leak its baked pool slices on shared boxes).
