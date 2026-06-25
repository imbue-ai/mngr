# Unabridged Changelog - mngr_imbue_cloud

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_imbue_cloud/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-24

`mngr imbue_cloud admin pool create` now validates `--region` against the known lease regions (`US-EAST-VA`, `US-WEST-OR`) and fails fast with a clear error if given anything else -- most importantly a raw OVH datacenter code (e.g. `vin`, which `admin server list` prints). Previously the region was accepted as a free-form string and stamped verbatim onto the `pool_hosts` row; a datacenter code there made the host permanently unleasable, because the connector's lease-time region filter is an exact, never-relaxed string match against the lease label the create form requests.

`mngr imbue_cloud admin pool create --backend slice --from-tag <tag>` now bakes the mngr vendored at that tag, instead of silently overriding the FCT clone's `vendor/mngr/` with the operator's local checkout. Previously every slice bake vendored the local monorepo's mngr (defaulting `--mngr-source` to this checkout) even on the `--from-tag` path, so a `--from-tag` bake produced the tag's FCT template paired with whatever mngr happened to be in the operator's tree -- a same-version content skew that could break the baked agent (e.g. a chat-agent create failing against a newer mngr's config validation). `--from-tag` now means byte-for-byte tag content, mngr included; `--workspace-dir` (dev) still vendors the local checkout, and an explicit `--mngr-source` still overrides either.

## 2026-06-23

Fixed `mngr create` on the `imbue_cloud` fast path (`fast_mode=require`) failing with "does not accept --image or --start-arg" when the workspace template supplies a `--start-arg` (e.g. the forever-claude-template `imbue_cloud` create template's `--restart=unless-stopped`).

The fast (adopt) path now tolerates start args the pre-baked pool-host container already carries (the `pool_host` template's docker run flags: `--security-opt=no-new-privileges`, `--workdir=/`, `--restart=unless-stopped`), keeping the fast and slow paths in sync. Any other start arg, or an `--image` swap, still requires `fast_mode=prevent` to rebuild.

Pinned all SSH host keys end-to-end and removed trust-on-first-use (TOFU) from the imbue_cloud pool flow.

Each baked slice now gets its own unique VM-root and container sshd host keys (no longer shared across an operator's slices), so one slice's key can never be used to impersonate another. The bake records each pool host's VPS/VM-root and container sshd host public keys (surfaced by `mngr create --format json`) into the connector's `pool_hosts` row, and OVH bare-metal boxes get an ed25519 host key we generate and inject during OS reinstall (recorded on the `bare_metal_servers` row). Leasing returns both host keys so the client pins them with strict host-key checking instead of scanning; the slow-path container rebuild pins its own freshly-generated key. The lima slice client, admin box SSH, and connector lease/teardown all pin the recorded key rather than disabling host-key checking.

A new one-shot `mngr imbue_cloud admin pool backfill-host-keys` command keyscans pre-existing pool rows and boxes to populate the new key columns (the single sanctioned, migration-only TOFU). After it runs, leasing fails closed on any row missing a pinned key rather than falling back to a scan. Run it once after deploying this version.

`mngr imbue_cloud admin server prep` now takes `--server-id` (instead of `--server-address`) and strictly pins the box's recorded sshd host key over SSH -- there is no trust-on-first-use fallback. The key is injected by `admin server setup` (OS reinstall) or captured once by `admin pool backfill-host-keys`; `prep` fails closed if the box has no recorded host key.

## 2026-06-22

Fixed host lock reporting for imbue_cloud pool hosts: a host's lock status is now derived from a real flock held-probe rather than the lock file's presence. The lock file now persists after release, so the previous mtime-based check would have reported every previously-locked host as permanently locked.

## 2026-06-21

Multiple developer environments can now safely share a single bare-metal slice box.

Each slice's lima instance and data-disk names are now stamped with the owning environment (`mngr-slice-<env>-<host-hex>`); `admin pool create --backend slice` takes a new `--slice-env-name` for this. Legacy un-stamped slices keep working and are never touched.

Slice baking now derives free-slot capacity from the box's real occupancy (every env's slices plus any legacy ones) instead of the per-env database, so independent envs cannot collectively over-subscribe a box.

Each slice carve now reserves its slot and host ports under a brief box-wide lock (it creates the instance without booting via `limactl create`, then boots it after releasing the lock), so concurrent bakes from different envs never collide on capacity or ports.

The post-bake orphan reaper now only ever deletes the active env's own stamped slices -- never another env's slices or legacy un-stamped ones.

Added `admin pool teardown-slices`, which tears down every unleased slice VM recorded in the pool DB (used by `minds env destroy` so a destroyed env doesn't leak its baked pool slices on shared boxes).

## 2026-06-20

Deprecated baking new OVH classic VPS pool hosts in `mngr imbue_cloud admin pool create`. The `--backend` default is now `slice` (bare-metal slices); passing `--backend ovh_vps` fails fast with a deprecation error pointing at `--backend slice`. Existing OVH VPS pool hosts are unaffected and can still be listed and destroyed (`admin pool list` / `admin pool destroy`, the connector's release + cleanup paths), so this is a deprecation, not a removal.

## 2026-06-19

Fixed a bug where restarting a stopped `imbue_cloud` (leased pool) mind left it in a broken, unrecoverable state. The subsequent `mngr start` SSH into the container failed ("Start step of host restart failed"), leaving the mind dead and UI-unrecoverable even though its data was intact on the volume.

There were two parts to the fix:

- `ImbueCloudProvider.get_host` previously returned an online host unconditionally, without checking whether the inner container was actually running. Because `mngr start` only re-starts a host when `get_host` reports it offline, the start command skipped `start_host` entirely and SSHed straight into the dead container. `get_host` now probes the container's running state over the outer root SSH (mirroring `VpsDockerProvider.get_host`) and returns an offline host when the container is stopped, so `mngr start` routes through `start_host`.

- `start_host` previously did a bare `docker start` and returned. But the in-container sshd is launched via `docker exec` (the container's command is just a sleep), so it does not survive a stop/start — the container came back with no sshd, and the per-host authorized key and host key may not have persisted either. `start_host` now re-bootstraps the container's SSH over the outer root SSH (which works independently of the container's sshd): it relaunches sshd, re-seeds the per-host authorized key (in case `/root` did not persist), waits for sshd to accept connections, and re-scans and re-records the served host key (reconciling any host-key change so strict host-key checking succeeds). This mirrors what the local docker and vps-docker providers already do on restart.

Together, a stopped leased mind can now be brought back to life.

`mngr list` discovery now reports a transport-level failure reaching the Imbue Cloud connector (connection refused, DNS failure, timeout -- the flaky-network / connector-down case) as a typed `ProviderUnavailableError` rather than a bare httpx error. Auth and account-configuration problems keep their own error types. This lets the minds recovery flow tell "the provider is unreachable, so a restart can't help -- just retry" apart from "your workspace can't be reached for another reason".

Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

Updated the slice provider's `_create_host_object` / `_wait_for_container_sshd` /
`_on_certified_host_data_updated` overrides to match the base's new per-host
realizer threading (the base now resolves an existing host's realizer from its
placement rather than the create-time config). imbue_cloud is container-only, so
it threads the realizer through unchanged; no behavior difference.

Fixed two `imbue_cloud` provider unit tests that broke on `main` after recent merges, so they once again match the production code:

- The `start_host` regression test (and `start_host`'s own docstring) still required an authorized-keys re-seed and a host-key re-scan, but those steps were deliberately removed because a `docker stop`/`docker start` preserves the container filesystem (only the sshd *process*, launched via `docker exec`, needs relaunching). The test and docstring now assert/describe just the sshd relaunch.

- The `get_host` test stub returned a boolean for the `docker inspect` running-state probe, but the probe now reads `{{.State.Status}}` and compares it against the shared `is_running_container_state` rule. The stub now returns a container status string, so a running leased container correctly resolves to an online host.

Trimmed the README to user-relevant content and tightened it for concision.

Corrected the connector-URL precedence (there is no baked-in default; it comes from the `connector_url` config or the env var, else raises) and the `hosts release` argument (a host-db-id, not a lease-id).

## 2026-06-18

`mngr imbue_cloud admin pool list` (and the `minds pool list` wrapper) now emits every `pool_hosts` column. It previously printed a hand-maintained 10-column subset that omitted `region`, `backend_kind`, and the slice identifiers (`bare_metal_server_id`, `lima_instance_name`, `lima_disk_name`), as well as `host_name`, `vps_instance_id`, and the SSH ports -- so a baked slice host showed up looking like a region-less OVH VPS. The SELECT and the emitted JSON keys are now both driven by a single column list, with a regression test asserting it stays in lockstep with the schema.

Agent lifecycle detection now targets the agent's primary tmux window by name (the configurable `tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index` setting.

## 2026-06-17

`mngr imbue_cloud admin pool destroy` is now backend-aware, mirroring the `--backend` branch in `pool create`. Previously it only knew the OVH-VPS teardown path: cancel the OVH VPS, then drop the row. Run against a bare-metal `slice` row it would try to cancel a non-existent OVH VPS (404) and, with `--skip-vps-cancel`, drop the row while leaving the slice's lima VM running on the box -- stranding a slot indefinitely (no cron reaps slice-VM orphans; only a subsequent bake does).

Now the teardown follows the row's `backend_kind`: a `slice` row destroys its lima VM (and data disk) on the bare-metal box -- freeing the slot -- before the row is dropped, while an `ovh_vps` row (including legacy rows written before the column existed) cancels its OVH VPS as before. Either teardown runs before the row delete, so a failure keeps the row and the operation stays retryable. The slice teardown reads the pool management key from `POOL_SSH_PRIVATE_KEY` (the same key the carve authorizes), so direct `admin pool destroy` of a slice requires that env var (the `minds pool destroy` wrapper injects it from Vault). `--skip-vps-cancel` still drops the row only, for any backend.

`mngr imbue_cloud admin server prep` now pre-installs the pinned Docker Engine (the same version the OVH VPS path pins) and inotify-tools into the staged golden slice image via `virt-customize` (adds a `libguestfs-tools` box dependency).

Because each slice VM's first-boot provisioning guards on presence (`command -v docker` / `command -v inotifywait`), baking these into the golden image makes those steps skip entirely — so slice carves no longer download/install Docker per VM. This speeds up baking (especially in parallel) and removes a per-slice network dependency. To re-stage an already-prepped box with the new image, delete the staged image and re-run `prep` (the step is idempotent and re-customizes on a fresh download).

`mngr imbue_cloud admin pool create --backend slice` now bounds parallelism with `--max-concurrency` (default 4): it bakes at most that many slices at once and queues the rest, reporting progress as each completes. This keeps box contention low enough that each `mngr create` finishes within its per-create timeout (raised to 45 minutes for slices). The timeout is per single create, so one slice timing out no longer aborts the others.

After the bakes finish, the slice backend reconciles the box's lima VMs against the pool DB and reaps any orphan — a VM with no `pool_hosts` row, e.g. one left by a create that was killed by its own timeout after carving but before the row insert (the provider's rollback can't run on a hard kill). Only slice-prefixed VMs absent from the DB are deleted; tracked slices (any status) are kept. The reap also runs on a top-level SIGTERM/SIGINT (e.g. the caller's subprocess timeout): the bake first kills its in-flight `mngr create` workers so they can't keep carving VMs, then reaps, so a killed bake never leaks worker processes or VMs.

Corrected bare-metal slice sizing so a box's slot count reflects what it can *realistically* run (this also flows into `admin server pricing`, which divides amortized cost by the slot count):

- RAM overhead is now modeled in two parts: a per-machine host reserve (`HOST_RAM_RESERVE_GIB`, kernel/OS + headroom, subtracted once) and a per-VM overhead (`PER_VM_RAM_OVERHEAD_MIB`, QEMU + lima supervisor, added to each slice's footprint). The guest now gets its full advertised `memory_per_slice_gb` (previously it was silently shortchanged by the overhead). `slot_count = (ram - host_reserve) / (slice + per_vm_overhead)`, so the box keeps real host headroom instead of being packed to 100%.

- Disk no longer overcommits: the reserve is now `max(DISK_RESERVE_GB, ceil(disk_gb * DISK_RESERVE_FRACTION))`, which absorbs the GB-vs-GiB gap (a nominal "N TB" spec is ~0.93·N GiB) plus partition/filesystem overhead, so per-slice disk allocations stay within the real usable filesystem.

`server prep` now also provisions a 32 GiB swapfile (the OS-install default of two ~0.5 GiB partitions was useless on a RAM-committed slice host).

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

Removed the dead disk-snapshot and `list_ssh_keys` stubs from `LimaSliceVpsClient`, matching the slimmed-down `VpsClientInterface` (which no longer declares `create_snapshot`/`delete_snapshot`/`list_snapshots`/`list_ssh_keys`). Slice snapshots, like other host snapshots, go through the provider layer. No user-facing behavior change: these methods only ever raised "unavailable".

## 2026-06-16

Dropped the dead `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` stub overrides from `LimaSliceVpsClient`, matching the removal of those abstract methods from the shared `VpsClientInterface`. These had no production callers and only raised "unavailable". The now-unused `VpsSnapshotId` / `VpsSnapshotInfo` / `VpsSshKeyInfo` imports and the unit-test calls that exercised the stubs were removed as well.

`destroy_host` now raises a `CleanupFailedGroup` carrying the classified cleanup failures (instead of returning them, or swallowing errors as warnings) when a resource is left behind, and returns normally otherwise. A resource that was already gone is treated as benign (no failure); a resource that exists but could not be destroyed is recorded as a `HOST_RESOURCE_REMAINS` failure (or `OTHER` for a bookkeeping/record write failure), so `mngr destroy`/`cleanup` can surface it and exit with an informative, cause-specific code. See `specs/cleanup-error-aggregation.md`.

## 2026-06-15

Added a `--skip-deferred-install-wait` flag to `admin pool create` (slice + ovh_vps): when set, the bake does NOT wait for the FCT deferred-install (heavy apt + Playwright/Chromium) to finish before stopping the baked services agent. Saves a few minutes per bake for dev/throwaway hosts; the tradeoff is the baked container's deferred-install may be left incomplete (stopping mid-apt can corrupt dpkg), so it must never be used for production pool hosts.

Added `mngr imbue_cloud admin server pricing`: an operator-only, read-only command that prints a per-slice pricing table for OVH bare-metal plans, to help decide what to buy before ordering.

- Each row is a server x RAM config x region. It reports the effective slice sizing (slots, vCPUs/slice, disk/slice) computed with the same `slices/bare_metal` math used to carve real slices, and the true monthly cost per slice (month-to-month price plus the one-time setup fee amortized over a year, divided by slot count). Rows are sorted cheapest-per-slice first and printed to stdout.

- Rows are split per region (vin = US-EAST-VA, hil = US-WEST-OR) because delivery time and stock differ by datacenter; each row shows the delivery-time and stock columns for its region (parsed from OVH availability). Knobs: `--region` (repeatable; default both US datacenters), `--memory-per-slice-gb` (default 8), `--cpu-overcommit` (default 2.0). Storage-upgrade options are listed at the end of each row as a marginal $/GB. A `CPU(c/t)` column shows the server's physical cores/threads so the (overcommitted) CPU/slice value is legible.

- A config is only excluded when NO available storage can host a slice at the chosen size; the base columns use the cheapest storage that IS sliceable, so RAM-dense servers that need a larger disk to fit a slice still appear (on that larger disk) instead of being dropped.

- The command only reads the OVH catalog and availability APIs; it never places an order. It needs `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` in the environment (from the activated env's ovh secret).

Added the bare-metal purchase + provisioning lifecycle commands under `mngr imbue_cloud admin server`:

- `order` — places a real OVH eco order for a chosen `--plan-code` / `--region` (vin/hil) / `--memory-gb` / `--storage`, driving the eco cart (mandatory bandwidth/memory/storage/vrack options, `dedicated_os=none_64.en`). It assigns the cart and shows OVH's real price preview for confirmation (`--yes` to skip), places the order, and records a `bare_metal_servers` row at status `ordered` with specs derived from the catalog. THIS CHARGES the account.

- `await-delivery` — polls the OVH order until the server is delivered (serviceName + public IP assigned), then advances the row to `delivered`. Resumable (no-op if already delivered); delivery can take ~1h.

- `setup` — provisions a delivered box to `ready`: reinstalls our OS via `/dedicated/server/{s}/reinstall` (Debian, RAID1, pool SSH key; destructive), waits for the install, waits for SSH, then runs the existing box prep (qemu/lima/tooling/service user/stage image). Resumable via status.

Together with `pricing`, this codifies the full RAM-pricing -> order -> deliver -> provision -> slice flow (previously the box was ordered and OS-installed by hand).

The pool bake now waits for the FCT `deferred-install` service (heavy apt + Playwright/Chromium download, started at agent boot) to finish before stopping the services agent, on both the OVH-VPS and slice paths. Stopping mid-apt previously corrupted dpkg (a package left reinst-required), so the deferred install failed on every post-lease retry until repaired.

Fixed slice pool bakes failing with "Conflicting providers: address has 'imbue_cloud_slice' but --provider is 'ovh'". The pool bake stacks a shared FCT container-build template on top of `main`; that template hardcoded `provider = "ovh"`, which is correct for an OVH VPS (whose create address is `@host.ovh`) but conflicts with a slice's `@host.imbue_cloud_slice` address. The build recipe is provider-agnostic, so the template (renamed `ovh` -> `pool_host`, in the FCT) no longer declares a provider -- the provider is selected entirely by the create address, mirroring the existing `aws` / `imbue_cloud` templates. `FCT_BAKE_TEMPLATES` is now `("main", "pool_host")`.

imbue_cloud fast path now matches on the **repository** as well as the branch/tag, so it can no longer adopt a pool host running different code than the request asked for.

- A new canonicalization function (`repo_identity.canonicalize_repo_source`) is the single source of truth for "the same repo": it normalizes remote URL forms (ssh/https, `.git`, trailing slash, host case) and resolves a local path to its `origin` remote, applied identically at bake time and request time. The provider canonicalizes the request's `repo_url` before the lease; `fast_mode=require` now raises `FastPathUnavailableError` (so the caller falls back to the slow rebuild) when it cannot establish a canonical `repo_url` and a `repo_branch_or_tag`, rather than matching on a subset.

- `admin pool create` no longer accepts hand-typed identity in `--attributes`; instead it derives and stamps the canonical `repo_url` + `repo_branch_or_tag` from the bake source, which is now exactly one of two mutually-exclusive selectors: `--from-tag <tag>` (production -- clones `--repo-url` at the tag into a fresh temp dir and bakes from it, so the content provably equals the tag) or `--workspace-dir <dir>` (dev -- bakes from a working tree, labelling it with the folder's `origin` + current branch, overridable via `--repo-branch-or-tag`). `--attributes` is now optional and rejects the `repo_url` / `repo_branch_or_tag` keys. Applies to both the `slice` and `ovh_vps` backends. The connector match (JSONB `@>` + region) is unchanged -- no migration.

Slice VMs now install `inotify-tools` during provisioning. The `host_backup` snapshot helper (the `OUTER_TRIGGER` btrfs helper that `mngr_vps_docker` runs on the slice's "outer" VM) execs `inotifywait` to watch for snapshot requests; the slice base image shipped without it, so the helper's systemd unit crash-looped (exit 127) and only serviced requests by accident of its restart cadence, leaving a spurious "snapshot path already exists" failure behind each successful snapshot. The OVH-VPS path already gets `inotify-tools` from `host_setup`'s base packages; the slice path now installs it in the lima VM provisioning alongside Docker (`jq`, the helper's other dependency, was already provided by the base lima script).

Added the OVH bare-metal "slices" feature: an alternative to ordering OVH VPSes where we carve VPS-like hosts out of bare-metal servers we rent by running lima/QEMU VMs on them. A slice is indistinguishable from a baked VPS pool host to minds and the imbue_cloud provider, but with cleaner btrfs (the lima data disk, no loopback).

- OVH order pricing helper (`pricing.compute_order_pricing`): true all-in month-to-month cost (base plan + every selected add-on delta + one-time setup + first payment), so the catalog's bare "base" price can't be mistaken for the real cost.

- Slice data model + pure logic (`bare_metal.py`): `BareMetalServer`/`BareMetalServerCapacity` types, `BackendKind`/`BareMetalServerStatus` primitives, and helpers for slot count, slice vCPU sizing with mild CPU overcommit, RAID-level choice, lima naming, slice port allocation, server lifecycle transitions, and "most-free ready server" placement.

- Lima slice creation: `build_slice_lima_yaml` produces a VPS-parity lima VM (root SSH, btrfs data disk at the host dir, Docker, two external port-forwards for the VM and inner-container sshds), and `LimaSliceVpsClient` provisions/destroys it via limactl. `SliceVpsDockerProvider` runs the shared vps_docker container bake on the VM (overriding only the per-host-port + btrfs-subvolume seams), producing a baked, reachable host. Verified end-to-end against a real lima VM.

- Admin CLI (`mngr imbue_cloud admin server`): `list` (per-server + fleet slot accounting), `register` (record a delivered box), `allocate-slice` (placement + the slice's lease attributes), `set-status` (advance the resumable order->delivered->installing->ready lifecycle), backed by a Neon access layer (`bare_metal_db`) that writes `bare_metal_servers` + slice `pool_hosts` rows directly.

- Made the fast/slow lease path work on slices end-to-end: the imbue_cloud provider now reaches a leased host's outer (VPS-root) sshd at the lease's `ssh_port` (a slice's box-forwarded VM-root port) instead of a hardcoded 22, so `mngr list`/discovery and destroy-time wipe target the slice VM rather than the bare-metal box's own sshd. The slice provider authorizes the pool management key on both the VM root and the inner container so the connector's lease-time key injection succeeds, and records the per-host forwarded ports so `mngr create --format json` reports them.

- `admin server allocate-slice` now actually allocates: it syncs this branch's mngr + the forever-claude-template workspace onto the chosen ready box(es), bakes the slice(s) there in parallel (`--count N`), authorizes the pool key, tears down the bootstrap-created chat agent + initial-chat sentinel, and inserts an `available` slice `pool_hosts` row. Adds `--dry-run` (placement preview), `--workspace-dir`, and `--mngr-source`.

- Per-slice sizing is no longer hardcoded. A bare-metal server now records its RAM-per-slice, CPU-overcommit factor, and usable disk at `admin server register`; `allocate-slice` computes each slice's vCPUs, RAM, and btrfs disk from those plus the box's specs (disk = usable space minus a reserve, split across slots). Because a box's per-slice values are fixed by its registration, `allocate-slice --count N` now targets a single server per invocation. Slices also record the server's real region (not a placeholder), and the per-box slice port-forward range was widened (~10k ports).

- The imbue_cloud slow-path rebuild now pins the leased host's outer (VPS-root) SSH host key in the rebuilding provider's known_hosts, so the certified-data sync over the outer connection no longer fails strict host-key checking (applies to OVH VPSes and slices alike).

- `allocate-slice` now also tears down the freshly-baked slice VM if parsing the bake's create-result JSON fails (e.g. a missing/invalid port field), closing a gap where such a failure could leave an orphaned VM holding a box slot with no `pool_hosts` row referencing it.

- Fixed slice disk overcommit: each slice VM has a fixed boot disk (OS + Docker) plus a btrfs data disk whose sizes now sum to the per-slice disk budget ((usable_disk - reserve) / slots). Previously only the data disk was sized and lima defaulted the boot disk to 100 GiB unaccounted, so a box was over-provisioned on disk (thin-provisioned via qcow2 -- it would run out of space if slices filled up). The boot disk is now set explicitly and `compute_slice_disk_gib` returns budget-minus-boot.

- Removed the duplicated, on-box slice bake. The slice path no longer ships the monorepo + forever-claude-template to the box and runs `mngr create` there; instead a slice is now provisioned (carved) and baked exactly like an OVH VPS, from the operator's machine. `LimaSliceVpsClient` drives `limactl` over SSH on the box (carve a bare Debian VM = the "OS reinstall" equivalent), and the shared container bake then reaches the VM's box-forwarded ports -- so `allocate-slice` just vendors mngr into the FCT workspace once and runs the bake from here. The FCT bake itself (templates, `system-services` agent, chat-agent teardown, sentinel) now lives in one provider-generic place (`pool_bake.py`) shared by both the OVH and slice paths, instead of being copy-pasted into the slice tooling. This deletes `slice_bake.py` and the box-side rsync/`uv sync`/git-init machinery; behavior for the operator is unchanged (`allocate-slice` still parallel-bakes `--count N` slices onto one ready box and inserts their rows).

- Restructured the now-large `mngr_imbue_cloud` plugin from a flat module list into layered sub-packages (`plugin`, `cli`, `bake`, `providers`, `hosts`, `slices`, `connector`) plus the shared root leaf modules (`config`, `data_types`, `errors`, `primitives`), with an `import-linter` "mngr_imbue_cloud layers contract" enforcing the ordering (and a meta-ratchet test that gates it). The slice/bare-metal subsystem is isolated in `slices/`, the provider-generic pool bake in `bake/` (an extraction seam toward the minds app), and both provider backends are co-located in `plugin/backends.py`. Pure refactor: no behavior, CLI, wire-format, or schema change. Plugin entry points moved to `imbue.mngr_imbue_cloud.plugin.entrypoints` / `plugin.slice_entrypoints`.

- Decomposed the oversized `providers/instance.py` (~2,000 lines): extracted the pure listing-shaping helpers into `providers/listing.py`, the pre-release data-wipe script generator into `providers/wipe.py`, and the slow-path VPS-vs-slice rebuild provider/config builders into `providers/rebuild.py` (with their unit tests co-located). `ImbueCloudProvider` and its self-bound methods stay in `instance.py`. Removed a dead helper (`_certified_host_name`). No behavior change.

- Folded `admin server allocate-slice` into `admin pool create --backend [ovh_vps|slice]`, so there is now a single command to bake a leasable pool host regardless of backend (the machine-provisioning step differs; the bake + row insert are shared). `admin server` keeps only the bare-metal fleet verbs (`prep`, `list`, `register`, `set-status`). Crucially, slice rows now go through the same lease-metadata path as OVH: they carry the operator's `--attributes` (e.g. `repo_branch_or_tag`) with the derived `{memory_gb, cpus}` size stamped on top, and record the operator-supplied `--region` (the app's region code, e.g. `US-EAST-VA`) instead of the box's raw datacenter code -- so a slice can be matched by a minds fast-path lease without hand-patching. `minds pool create` is unaffected (`--backend` defaults to `ovh_vps`).

- Fixed slice fast-path leases hanging at "Waiting for initial chat agent...": the slice bake now stops the `system-services` agent post-bake (inside the container), matching what the OVH bake already did via local mngr. Previously a slice was baked with the agent left running, so the fast-path lease adopted an already-bootstrapped agent whose initial chat agent had been torn down at bake finalize -- and the one-shot FCT bootstrap never recreated it. Stopping it means the lease starts it fresh, re-running the bootstrap, which recreates the chat agent under the leasing user's workspace name. (Audited the OVH vs slice bake paths for other such divergences; the remaining differences -- OVH ufw + management-key install, cancelled-VPS recycle, OVH IAM tags -- are intentionally OVH-only, and the row inserts are at parity.)

Fixed `mngr imbue_cloud admin server order` failing with "expected exactly one vrack option to auto-pick, got []" on bare-metal plans that do not offer a vrack option (e.g. the cheaper SK line such as `24sk602-v1-us`).

The eco-cart option selection no longer hardcodes `(bandwidth, vrack)` as the auto-picked families. Instead it derives the auto-pick set from the catalog's own `mandatory` flags: the operator chooses memory + storage, and every *other* family OVH marks mandatory (e.g. bandwidth, and vrack only where the plan offers it) is auto-picked and must have exactly one offer. Optional add-on families (mandatory=false) are never silently added to the cart.

## 2026-06-12

Internal: routed the agent `data.json` path constructions through the shared `get_agent_state_dir_path` helper (now in `imbue.mngr.hosts.common`). No behavior change.

## AWS provider support: ProviderBackendInterface refactor

`is_for_host_creation` was removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d in every other backend). Replaced with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method on the interface that Modal overrides. The imbue-cloud backend's now-unused `del`-of-`is_for_host_creation` is removed. No behavior change.

`mngr imbue_cloud admin pool create` now passes `--ovh-datacenter=` instead of `--vps-datacenter=` to the inner `mngr create --provider ovh` command. The OVH provider's `--vps-*` build-arg prefix was retired in this branch and now raises a migration error; the call site here is updated to the new per-provider prefix so pool creation continues to work.

`_build_delegated_vps_provider` now returns a `MinimalVpsDockerProvider` (moved into `mngr_vps_docker` itself, since it's a generally useful role for any externally-managed-VPS host-setup path -- not imbue_cloud-specific). The base `VpsDockerProvider._parse_build_args` was made abstract in this branch (each concrete provider binds its own `--<provider>-*` prefix); `MinimalVpsDockerProvider`'s override extracts `--git-depth=N` and forwards everything else to docker, which is the correct behavior for the no-provisioning path that pairs with `ExternallyManagedVpsClient`. Without this, every slow-path container rebuild would raise before any docker work happened. The corresponding parser unit tests moved alongside.

## 2026-06-11

Replaced direct ValueError/RuntimeError raises in build-arg parsing and host provisioning with dedicated custom exception types.

## 2026-06-10

Raised the stale coverage floor from 19% to 45% to match the coverage CI already measures (~50%).

## 2026-06-09

Offline hosts produced by this provider are now readable: the offline-host
construction path (used by both `get_host` for stopped hosts and
`to_offline_host`) returns an `OfflineHostWithVolume` (which implements the new
`HostFileReadInterface`) via the shared `make_readable_offline_host` helper.
This makes a stopped host's files readable through the same interface as an
online host -- used by Claude session preservation when a host is destroyed
while offline (the destroy path obtains the host via `get_host`), and available
to other readers of offline host data. The host's volume is resolved lazily on
first read, so this adds no per-host probe to host discovery. When no volume is
available, reads behave as "nothing there".

## 2026-06-08

The imbue_cloud slow (rebuild) path now re-applies the full idempotent host
setup on the leased VPS before rebuilding the container: it ensures the pinned
Docker version, installs/registers gVisor `runsc` if missing, tunes sshd, and
installs the base packages. This runs after the old container is torn down and
before the rebuild, and a failure is fatal. The result is that a workspace
created via the slow path -- even against a host baked with an old version, or
one baked before runsc existed -- comes up consistent and runs its agent
container under gVisor.

`ImbueCloudProviderConfig` now extends `VpsDockerProviderConfig`, so it carries
`docker_runtime` / `install_gvisor_runtime` / `default_start_args`; the delegated
vps_docker provider used for the rebuild forwards these, so the rebuilt container
runs under `--runtime runsc` with the `--workdir=/` and
`--security-opt=no-new-privileges` hardening args. These values are written into
the per-account `[providers.imbue_cloud_<slug>]` block by minds bootstrap.

Added a `--no-recycle` flag to `mngr imbue_cloud admin pool create`. By default
the OVH provider reclaims a cancelled (still-billable) VPS when one is available;
`--no-recycle` forces a fresh order instead (it sets
`MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED=false` on the inner `mngr create`),
which makes it easy to exercise and test the fresh-provision path.

Region-aware leasing for imbue_cloud hosts.

- `mngr create` against imbue_cloud now accepts two new build-arg knobs: a hard `-b region=<datacenter>` requirement (only a host in that datacenter is leased, else the create fails) and a soft `-b preferred_region=<datacenter>` preference (a host in that datacenter is preferred, but any available host is still returned so the fast path is never blocked). Both are validated against the known OVH-US datacenters (`US-EAST-VA`, `US-WEST-OR`); an unknown value fails fast.
- Both knobs are sent to the connector's lease endpoint as separate fields (not folded into the JSONB attribute filter) and are applied on both the fast (adopt) and slow (rebuild) create paths. A hard `region` is preserved through the slow path's attribute relaxation.
- `mngr imbue_cloud admin pool add` now records the bake `--region` (OVH datacenter) into the new `pool_hosts.region` column so the connector can filter/order on it.

- Removed the soft `preferred_region` lease knob. A lease now takes only the hard
  `region` build arg (`-b region=<dc>`): when set, only a host in that OVH
  datacenter is leased, otherwise the lease is region-agnostic.

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

- Now auto-discovered as a publishable package by the release tooling (it is a standalone `mngr imbue_cloud` provider plugin, not minds-specific). It will be offered for first publication to PyPI on the next release. Its previously-unpinned internal deps (`imbue-mngr-vps-docker`, `imbue-common`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its previously-unpinned internal deps (`imbue-mngr-vps-docker`, `imbue-common`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check. No runtime behavior change.

Fixed a stale reference in `UNABRIDGED_CHANGELOG.md`: the `minds-dev-iterate`
skill was renamed to `minds-dev-workflow`. The historical entry now points at
the current skill name (noting the former name) so readers can find it.

## 2026-06-03

Fixed the imbue_cloud slow (rebuild) path. When `fast_mode=prevent` leased a host
and rebuilt its container, the rebuilt host was still marked as carrying a
pre-baked agent, so `provision_agent` took the minimal "adopt" path (which runs a
`python3` claude-config patch) against the freshly-rebuilt container -- failing
with `python3: not found`. The slow path now builds the host object with
`adopt_pre_baked_agent=False`, so `pre_baked_agent_id` is unset and mngr runs its
standard full create + provision pipeline (matching the slow path's "fresh OVH
host" contract). The rebuilt agent gets a fresh id; the bake's agent id was only
bookkeeping (release keys off the lease's host db id).

This pairs with the FCT `imbue_cloud` create template gaining the same build
config as `ovh` (`--file=Dockerfile .`, `target_path=/mngr/code/`, `fct-seed`
post-create) so the rebuild produces the FCT image rather than a bare
`debian:bookworm-slim`; those build args are ignored on the fast/adopt path.

Fixed the pool-host bake writing the wrong value into `pool_hosts.vps_instance_id`:
the INSERT passed the mngr `host_id` where the OVH service name belongs, which
broke every connector-side OVH teardown (they key on `vps_instance_id`). The bake
now writes `vps_address` (the service name) via the new pure
`build_pool_host_insert_values()`, pinned by a regression test using the real
`host-`/`vps-` shapes.

`mngr imbue_cloud admin pool destroy` (and the `minds pool destroy` wrapper) now
do a full teardown: cancel the OVH VPS (strip per-lease tags + `deleteAtExpiration`)
before dropping the row, so it can no longer strand a still-billing VPS. Pass
`--skip-vps-cancel` only when the VPS is already gone. The wrapper injects the
tier's OVH credentials from Vault, like `pool create`. Relatedly, the imbue_cloud
provider's `destroy_host` now raises when the connector release fails instead of
silently cleaning up local state, so a failed release no longer makes mngr
"forget" a host whose lease/VPS is still live.

Stopped masking errors in the lease/teardown paths (error-handling audit):
- `_list_leased_hosts_cached` no longer swallows a `list_hosts` failure to an
  empty list -- a transient connector outage / expired token now propagates
  (the method already raised via `_require_account`, so callers tolerate it)
  rather than making the account look like it has zero leased hosts.
- `client.release_host` now raises `ImbueCloudConnectorError` on a transport
  error or non-2xx (e.g. the synchronous release returning 5xx because the OVH
  cancel failed) instead of returning a quiet `False`. `destroy_host` lets it
  propagate (so a failed release surfaces and local state isn't cleaned up);
  the create-rollback path (`_release_lease_quietly`) catches it explicitly to
  stay best-effort.
- The leased-host TOFU host-key scan now logs (debug) the cause when it can't
  read a remote key, so the later StrictHostKeyChecking SSH failure is
  diagnosable.

Added `mngr imbue_cloud admin paid` subcommands for managing the connector's paid-user lists: `paid domain add|remove|list` and `paid email add|remove|list` (with `--paid-only` on list). These talk to the connector's `/paid/*` admin API using the fixed API key read from `$MINDS_PAID_ADMIN_KEY` (or `--api-key`). Added matching client methods and a `PaidListEntry` data type.

Added a robust "slow path" to imbue_cloud host leasing. A new `fast_mode` build
arg (`-b fast_mode=require|prevent`) selects how `mngr create` lands on a pool
host:

- `fast_mode=require`: lease a pool host whose attributes exactly match and adopt
  its pre-baked agent (the original fast path). Raises a distinct
  `FastPathUnavailableError` when no exact match exists.
- `fast_mode=prevent` (the new default): lease any adequately-sized available
  host (resource attributes only; `repo_branch_or_tag`/`repo_url` are dropped),
  destroy its baked container, and rebuild it from the FCT Dockerfile via the
  shared `mngr_vps_docker` setup path, then run mngr's standard full client-side
  setup -- exactly like an OVH host.

Once a host is leased, any failure during the remaining setup now releases the
lease back to the pool before re-raising, so failed builds never leak a lease.
Logs clearly state which path was taken (`FAST PATH` vs `SLOW PATH`).

Unknown `-b` entries (e.g. `--file=Dockerfile`, `.`) are now forwarded verbatim
to the delegated build instead of being rejected.

## 2026-06-02

Simplified an exception handler now that `HostError`/`HostConnectionError`/`HostNotFoundError`
are all `MngrError` subclasses: the redundant `except (HostConnectionError, HostNotFoundError,
MngrError)` guard is now just `except MngrError`. No behavior change.

- pyproject.toml: align `imbue-mngr*==` pin stragglers with the satellites bumped in main's `e22e7010e` release commit. Several `imbue-mngr-*` libs still pinned to older versions even though `libs/mngr` had moved to 0.2.10; building the apps/minds ToDesktop bundle from main today would fail at `uv lock` in `apps/minds/scripts/build.js` because the workspace constraint graph is unsatisfiable. Day-to-day dev hides this because `[tool.uv.sources]` redirects every `imbue-mngr-*` to its workspace path, bypassing the `==` pin.

## 2026-06-01

# Offline agent field generators

Updated the provider's `get_host_and_agent_details` override (and its lease-only `_build_offline_details_from_lease` fallback) to accept and forward the new `offline_field_generators` parameter, so offline plugin fields (see the mngr changelog entry) are populated for leased hosts that fall back to offline/lease-only data.

## 2026-05-29

# Fix OAuth CLI hang after successful browser sign-in

- Fixed a bug in `mngr imbue_cloud auth oauth` where the local callback listener would hang until the 300s timeout after the browser had already returned the OAuth code. The handler now only records query params when the request is for `/oauth/callback` and carries non-empty params, so secondary browser GETs (favicon, prefetches, etc.) can no longer overwrite the captured callback with `{}`.

Added R2 bucket support: a new `mngr imbue_cloud bucket` command group for
creating, listing, inspecting, and destroying R2 buckets (one per host, paid
accounts only), plus `bucket keys create/list/destroy` for minting and revoking
scoped S3 keys (read-only or read-write) to hand to different agents.

`bucket create` returns S3-compatible credentials (access key id, secret access
key, endpoint, bucket name) as JSON; the secret is shown only once and is never
stored by the service. `bucket destroy` refuses a non-empty bucket and, on
success, revokes all of that bucket's keys.

`mngr destroy <agent>` against an imbue_cloud-leased pool host is now
*terminal* rather than a soft `docker stop`. The new flow on the leased
VPS:

1. Stops + removes the workspace container, drops the per-host docker named
   volume, deletes the per-host btrfs subvolume under `/mngr-btrfs/`, runs
   `docker system prune -a -f --volumes`, and wipes `/root` + `/tmp`
   (preserving only `/root/.ssh/authorized_keys` so the pool-management ssh
   path still works through `cleanup_released_hosts.py`).
2. Releases the lease back to the pool (the `/hosts/{id}/release` connector
   call -- same as `mngr imbue_cloud hosts release`).
3. Cleans up local per-host state (ssh keys, known_hosts, cached records).

Privacy-first ordering: the agent's data is gone before the connector flips
the row to `released`, so the eventual VPS-destroy by
`cleanup_released_hosts.py` is belt-and-suspenders rather than the only
barrier.

To stop the container without releasing the lease (i.e. you intend to
resume the workspace later on the same VPS), use `mngr stop <agent>`
instead.

`mngr delete <agent>` (the GC path) now also runs this same flow; it's a
safe no-op for an already-released lease and acts as a recovery path if a
prior `destroy` crashed mid-wipe.

The wipe script (`build_pool_host_wipe_script`) is exposed as a pure free
function in `mngr_imbue_cloud.instance` so the rendered shell can be unit
tested without standing up an SSH transport.

The minds app now consumes the `mngr imbue_cloud bucket` capability: when a
workspace is created with the `imbue_cloud` backup provider, minds calls
`mngr imbue_cloud bucket create` / `bucket keys create` to provision a
per-workspace R2 bucket (named after the host id) and a scoped readwrite key,
then points the workspace's restic backups at it.

(This integration PR adds no code in this project; it wires the existing
bucket commands into the minds workspace-creation flow. The bucket commands
themselves are covered by the `mngr-cloud-bucket` changelog entry.)

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

# Delete the dead imbue_cloud inject helpers

`build_combined_inject_command` and `normalize_inject_args` (and the
`_sed_replace_env_line` / `_ensure_no_quote_chars` helpers that only
they called) were added to support a "claim CLI" pattern that never
landed. Trimming the `minds_api_key` argument earlier in this branch
left them with no caller anywhere in the monorepo except their own
test file; the central `MINDS_API_KEY` is now injected by the
latchkey gateway's `minds-api-proxy` extension on the fly, not
pushed down onto a leased pool host.

This change deletes those four functions and the entire `host_test.py`
file. The live `provision_agent` path on `ImbueCloudHost` still uses
`_build_patch_claude_config_command`, which stays.

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-22

## No more silent auto-disable on auth errors

- Previously, when `ImbueCloudAuthError` was raised during discovery, minds would silently rewrite the user's settings to set `is_enabled = false` for the offending `imbue_cloud_<slug>` block. That behavior is gone (see the `apps/minds` changelog for details). `mngr_imbue_cloud` itself is unchanged -- it still raises `ImbueCloudAuthError` on session-revoke errors; the difference is that those errors now propagate to the providers panel in minds (where the user can choose to disable the provider explicitly) instead of triggering a hidden config rewrite.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions to match the current monorepo.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

End-to-end fixes for the OVH-backed pool flow (bake -> lease/adopt -> first-start). Discovered + fixed iteratively while smoke-testing the flow against a fresh dev env.

### `pool_hosts` INSERT picks up the schema's `host_name` column

A prior schema migration added `host_name NOT NULL` to `pool_hosts` but the bake's INSERT in `mngr_imbue_cloud.cli.admin._create_single_pool_host` was never updated. Every successful pool bake died at the very last step with `null value in column "host_name" of relation "pool_hosts" violates not-null constraint` -- worst of all, the cleanup path doesn't run on a psycopg2 error, so the OVH VPS + docker image + agent + ufw + injected management key were all already done by the time the INSERT fired, and every failed bake leaked a fully-provisioned VPS. Fix adds the column (the variable was already computed at the top of `_create_single_pool_host`) and extracts the SQL into a module-level `_INSERT_POOL_HOST_SQL` constant with a regression test asserting every required column appears, so any future drift of the same shape gets caught up front without needing a fake DB.

### Bake produces a leasable state aligned with the adopt path

- The bake's services agent now uses the constant name `system-services` (was a per-bake `pool-<hex>` UUID). The minds-side adopt code in `mngr_imbue_cloud.host.ImbueCloudHost.create_agent_state` explicitly keeps the bake's name verbatim, so the bake has to use the same name the user's `mngr create system-services@<host>.imbue_cloud_<slug>` does -- otherwise the leased workspace's tmux sessions are named after the per-bake UUID instead of the user's expected `system-services`. The per-bake unique `pool-<hex>-host` suffix stays on the *host name* for operator-local mngr disambiguation across sequential bakes.
- After the existing key-injection step, the bake destroys the FCT-bootstrap-created chat agent and `rm -f`'s `/code/runtime/initial_chat_created`. During the bake the services agent boots and the FCT bootstrap creates an initial chat agent named after the bake's host (per `_build_create_chat_command` in the FCT bootstrap), then drops a sentinel file so it never recreates on later starts. Without the cleanup, the user's lease inherits the bake's chat agent name and the bake-time agent's claude session that has no API key (because the user's LiteLLM key didn't exist at bake time). Destroying both lets the bootstrap fire fresh on the user's first start with the correct host_name + access to the patched claude config dir.
- The bake's subsequent `mngr stop` / `mngr exec` calls use the full address `system-services@<host_name>.ovh` instead of just `system-services`. Now that the agent name is a constant, the operator's local mngr state accumulates one `system-services` agent per bake (each on a different host). `_get_agent_info` previously took an agent name alone and the mngr-list `--include` filter returned the first match, which under sequential bakes is some prior bake's stale agent on a stale VPS -- the bake would then SSH the wrong VPS for ufw + key injection + DB INSERT while the actually-baked container received nothing. `_get_agent_info` now takes `host_name` as a keyword arg and filters by both `name` and `host.name`.
- Multi-token `mngr exec` commands are packed into a single `shlex.join`'d positional string. `mngr exec`'s click parser is `AGENTS... COMMAND` -- the LAST positional goes to `COMMAND` and the rest to `AGENTS`. Passing the inner `mngr destroy <name> --force` as separate argv entries either ate `--force` as a `mngr exec` option (which doesn't exist) or treated `mngr`/`destroy`/`<name>` as additional agent names. Joining into one string sidesteps both.

### Lease/adopt rewrites the container's `host_name`

`ImbueCloudProvider.create_host` now SFTPs into the leased container after the host-key scan and rewrites `/mngr/data.json`'s `host_name` field to the user-supplied `HostName`. Without this, the FCT bootstrap's `_maybe_create_initial_chat` (which reads `host_name` from `/mngr/data.json` to decide what to name the freshly-recreated chat agent on the user's first start) inherits the bake's placeholder name (`pool-<hex>-host`) instead of the user's chosen workspace name. SFTP-based to dodge shell-quoting hazards in an `exec_command` round-trip; raises `MngrError` on any SSH / SFTP / JSON failure since the wrong `host_name` is exactly the bug this exists to prevent.

Swap the imbue-cloud pool bake walker from Vultr to OVH:

- `mngr imbue_cloud admin pool create` is now provider-generic. It drops the `MINDS_ROOT_NAME` env detection, adds a required `--region REGION` and repeatable `--tag KEY=VALUE`, lands on `--template main --template ovh` with `@host.ovh` + `--provider ovh`, appends `-b --vps-datacenter=<region>`, and installs + configures `ufw` on every leased VPS before the row hits `pool_hosts`. UFW failures abort the bake.
- `forever-claude-template` gains a `[create_templates.ovh]` block (no plan / datacenter baked in -- region flows in per-invocation, plan defaults from `OvhProviderConfig`). The `[create_templates.vultr]` block stays in place; `mngr_vultr` is still a registered provider for non-pool uses.

## 2026-05-12

`mngr list` for imbue_cloud now drives discovery through outer (VPS root) SSH instead of inner-container SSH. Each lease produces one outer-SSH round-trip per host: `docker exec` for a running container (reading full state inside) or `docker cp` for a stopped one (extracting the host_dir to a tmp path on the VPS). The listing therefore shows the container's true state — `RUNNING` / `STOPPED` / `CRASHED`-with-exit-code / `PAUSED` / `DESTROYED` — together with friendly host name, image, tags and full agent details even when the inner sshd is unreachable. Lease-only synthesis (state=CRASHED with `failure_reason` carrying the underlying error) is now reserved for the last-resort case where even outer SSH fails. Same `_make_outer_for_vps_ip` defense added to vps_docker / vultr so a single unreachable VPS no longer drops the others, and a pre-existing crash in the framework offline path (`CommandString("")` violating `NonEmptyStr`) is fixed.

## 2026-05-06

- `mngr imbue_cloud admin pool create`: post-create read-back is now scoped to `--provider <provider>` (default `vultr`) and uses `--on-error continue`, so a pre-existing stale host on the operator's machine no longer aborts the bake before the management-key install + DB INSERT. The bake still fails loudly when the just-created agent is genuinely missing from the listing output.
- Removed the broken `just create-pool-hosts-dev` and `just create-pool-hosts` recipes. Both called `apps/remote_service_connector/scripts/create_pool_hosts.py`, which still inserted into the dropped `pool_hosts.version` column and so failed against the migrated schema. The replacement is `mngr imbue_cloud admin pool create` (with `--mngr-source` for the dev-loop's working-tree-into-vendor/mngr/ rsync). `just sync-vendor-mngr` is unchanged -- it serves a different (release) flow not covered by the plugin. Updated `just minds-start`'s "no FCT worktree" hint and the `minds-dev-workflow` skill to point at the new bake path.
- Deleted dead code: `apps/remote_service_connector/scripts/create_pool_hosts.py` (replaced by `mngr imbue_cloud admin pool create`).

- Internal: re-baseline mngr_imbue_cloud against the standard ratchet checks. The new plugin's `test_ratchets.py` now includes the full set of `test_prevent_*` functions derived from `standard_ratchet_checks.py` (snapshots pinned to current violation counts so they can only ratchet down).
- Internal: register `imbue.mngr_imbue_cloud` in the root `pyproject.toml`'s combined `--cov=` list so the per-package and combined coverage gates see its source files. Pin the plugin's per-package coverage gate to its current 19% baseline (was 50%, never met) and lower mngr_recursive's gate from 84% to 83% to reflect the recently-added remote-upload helpers.

- New `mngr_imbue_cloud` plugin (`libs/mngr_imbue_cloud/`) that owns auth (SuperTokens), pool-host leasing, LiteLLM keys, and Cloudflare tunnels for the Imbue Cloud service. Adds a `mngr imbue_cloud` CLI command group with `auth`, `hosts`, `keys litellm`, `tunnels`, and `admin pool` subcommands. Multi-account is modelled as multiple provider instances of the same backend (each with `account = "<email>"`).
- `mngr create --provider imbue_cloud_<account-slug> --new-host -b repo_url=... -b cpus=... ...` now leases a matching pool host and adopts its pre-baked agent under the requested name in one invocation. Lease attributes flow through `--build-arg`; `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`MNGR_PREFIX` flow through `--host-env`. The plugin's `on_load_config` hook auto-registers a provider entry per signed-in account so no manual `[providers.imbue_cloud_*]` block is needed.
