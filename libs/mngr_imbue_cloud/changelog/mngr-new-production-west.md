`mngr imbue_cloud admin server order` now lets you order plans whose mandatory
option families (e.g. bandwidth, vrack) offer more than one choice. Previously the
cart build failed with "expected exactly one X option to auto-pick" on such plans
(e.g. the `24sys*` SYS line). Choose the offer per family explicitly with the new
repeatable `--option <planCode>` flag; single-offer families are still auto-selected.
Run `order` without it once and the error lists each ambiguous family's offers and
their monthly prices so you can re-run with the right `--option` values.

`mngr imbue_cloud admin pool create --backend slice` now requires `--server-id`
(the bare-metal box to bake the slices onto, from `admin server list`). It no
longer auto-selects the box with the most free slots -- baking always targets an
explicitly-chosen, ready server.

Fixed a bare-metal box-prep bug that made every slice bake fail with `mkdir
~/.cache/lima: permission denied`. The prep script (run as root) staged the slice
base image under the lima user's `~/.cache` but left `~/.cache` itself root-owned,
so `limactl` (run as the lima user) could not create `~/.cache/lima`. Prep now
creates and chowns the cache dir chain to the lima user (and repairs an
already-root-owned `~/.cache` when re-run on an existing box).

The post-bake orphan reap now also reaps leaked lima **data disks**, not just VM
instances. A failed carve whose rollback `limactl delete` could not unlock the
disk leaves the disk behind (the VM is gone but the disk keeps holding the box
slot); the reap now reconciles the box's disks against the pool DB and force-deletes
(unlocking first) any slice disk with no row.
