`mngr imbue_cloud admin server prep` now pre-installs the pinned Docker Engine (the same version the OVH VPS path pins) and inotify-tools into the staged golden slice image via `virt-customize` (adds a `libguestfs-tools` box dependency).

Because each slice VM's first-boot provisioning guards on presence (`command -v docker` / `command -v inotifywait`), baking these into the golden image makes those steps skip entirely — so slice carves no longer download/install Docker per VM. This speeds up baking (especially in parallel) and removes a per-slice network dependency. To re-stage an already-prepped box with the new image, delete the staged image and re-run `prep` (the step is idempotent and re-customizes on a fresh download).

`mngr imbue_cloud admin pool create --backend slice` now bounds parallelism with `--max-concurrency` (default 4): it bakes at most that many slices at once and queues the rest, reporting progress as each completes. This keeps box contention low enough that each `mngr create` finishes within its per-create timeout (raised to 45 minutes for slices). The timeout is per single create, so one slice timing out no longer aborts the others.

After the bakes finish, the slice backend reconciles the box's lima VMs against the pool DB and reaps any orphan — a VM with no `pool_hosts` row, e.g. one left by a create that was killed by its own timeout after carving but before the row insert (the provider's rollback can't run on a hard kill). Only slice-prefixed VMs absent from the DB are deleted; tracked slices (any status) are kept. The reap also runs on a top-level SIGTERM/SIGINT (e.g. the caller's subprocess timeout): the bake first kills its in-flight `mngr create` workers so they can't keep carving VMs, then reaps, so a killed bake never leaks worker processes or VMs.

Corrected bare-metal slice sizing so a box's slot count reflects what it can *realistically* run (this also flows into `admin server pricing`, which divides amortized cost by the slot count):

- RAM overhead is now modeled in two parts: a per-machine host reserve (`HOST_RAM_RESERVE_GIB`, kernel/OS + headroom, subtracted once) and a per-VM overhead (`PER_VM_RAM_OVERHEAD_MIB`, QEMU + lima supervisor, added to each slice's footprint). The guest now gets its full advertised `memory_per_slice_gb` (previously it was silently shortchanged by the overhead). `slot_count = (ram - host_reserve) / (slice + per_vm_overhead)`, so the box keeps real host headroom instead of being packed to 100%.

- Disk no longer overcommits: the reserve is now `max(DISK_RESERVE_GB, ceil(disk_gb * DISK_RESERVE_FRACTION))`, which absorbs the GB-vs-GiB gap (a nominal "N TB" spec is ~0.93·N GiB) plus partition/filesystem overhead, so per-slice disk allocations stay within the real usable filesystem.

`server prep` now also provisions a 32 GiB swapfile (the OS-install default of two ~0.5 GiB partitions was useless on a RAM-committed slice host).
