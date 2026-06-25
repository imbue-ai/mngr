# Changelog - mngr_imbue_cloud

A concise, human-friendly summary of changes for the `mngr_imbue_cloud` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: OVH bare-metal **"slices"** — an alternative to OVH VPSes where pool hosts are carved out of bare-metal servers we rent, by running lima/QEMU VMs on them. A slice is indistinguishable from a baked VPS pool host to minds and the imbue_cloud provider, but with cleaner btrfs (the lima data disk, no loopback). Adds a new `mngr imbue_cloud admin server` lifecycle (`order` / `await-delivery` / `setup` / `prep` / `list` / `register` / `set-status` / `pricing`), and the slice bake flows through `mngr imbue_cloud admin pool create --backend slice`.
- Added: `mngr imbue_cloud admin server order` places a real OVH eco order (`--plan-code` / `--region` / `--memory-gb` / `--storage`). **THIS CHARGES the account.** Mandatory option families are auto-picked from the catalog; ambiguous families are exposed via repeatable `--option <planCode>` (running `order` once without it lists each ambiguous family's offers and prices). Companion `await-delivery` polls until delivered (~1h) and `setup` provisions the box to `ready` (OS reinstall via `/dedicated/server/{s}/reinstall`, then box prep).
- Added: `mngr imbue_cloud admin server pricing` — read-only per-slice pricing table for OVH bare-metal plans, sorted cheapest-per-slice first. Each row is a server × RAM config × region and reports effective slot count, vCPUs/slice, disk/slice, and true month-to-month cost per slice. Knobs: `--region` (repeatable), `--memory-per-slice-gb` (default 8), `--cpu-overcommit` (default 2.0). Reads only the OVH catalog / availability APIs; never places an order.
- Added: `mngr imbue_cloud admin pool create --backend slice --max-concurrency N` (default 4) bounds slice-bake parallelism — bakes at most N at once and queues the rest. The per-create timeout is raised to 45 minutes for slices. After bakes finish, the slice backend reconciles lima VMs and **lima data disks** against the pool DB and reaps any orphan (also runs on SIGTERM/SIGINT).
- Added: `mngr imbue_cloud admin pool create --backend slice --slice-env-name <env>` stamps the owning environment into each slice's lima names (`mngr-slice-<env>-<host-hex>`); paired with the new `mngr imbue_cloud admin pool teardown-slices` command (tears down every unleased slice VM recorded in the pool DB), multiple developer environments can safely share a single bare-metal slice box. Each slice carve reserves its slot and host ports under a brief box-wide lock so concurrent bakes from different envs never collide. Legacy un-stamped slices keep working and are never touched.
- Added: `mngr imbue_cloud admin pool create --backend slice --from-tag <tag>` now bakes the mngr vendored at that tag instead of silently overriding the FCT clone's `vendor/mngr/` with the operator's local checkout — so a `--from-tag` bake is byte-for-byte tag content, mngr included.
- Added: `mngr imbue_cloud admin pool backfill-host-keys` — one-shot command that keyscans pre-existing pool rows and boxes to populate the new SSH host-key columns; the single sanctioned, migration-only TOFU. After it runs, leasing fails closed on any row missing a pinned key rather than falling back to a scan.
- Added: `--skip-deferred-install-wait` flag on `admin pool create` (slice + ovh_vps) — when set, the bake does not wait for the FCT deferred-install (heavy apt + Playwright/Chromium) to finish before stopping the baked services agent. Saves a few minutes per bake for dev/throwaway hosts; must never be used for production pool hosts (stopping mid-apt can corrupt dpkg).
- Added: The imbue_cloud fast path now matches on the **repository** as well as the branch/tag, so it can no longer adopt a pool host running different code than the request asked for. A new `repo_identity.canonicalize_repo_source` normalizes remote URL forms (ssh/https, `.git`, trailing slash, host case) and resolves a local path to its `origin` remote; applied identically at bake time and request time. `admin pool create` no longer accepts hand-typed identity in `--attributes`; it derives canonical `repo_url` + `repo_branch_or_tag` from the bake source, which is now exactly one of `--from-tag <tag>` (production) or `--workspace-dir <dir>` (dev).
- Added: A 32 GiB swapfile is now provisioned on bare-metal boxes by `admin server prep`.

### Changed

- Changed: **SSH host keys pinned end-to-end, removing TOFU from the imbue_cloud pool flow.** Each baked slice gets unique VM-root and container sshd host keys (no longer shared across an operator's slices). The bake records each pool host's VPS/VM-root and container sshd host public keys into the connector's `pool_hosts` row, and OVH bare-metal boxes get an ed25519 host key generated and injected during OS reinstall (recorded on the `bare_metal_servers` row). Leasing returns both host keys so the client pins them with strict host-key checking instead of scanning; the slow-path container rebuild pins its own freshly-generated key. The lima slice client, admin box SSH, and connector lease/teardown all pin the recorded key rather than disabling host-key checking. `mngr imbue_cloud admin server prep` now takes `--server-id` (instead of `--server-address`) and strictly pins the box's recorded sshd host key — there is no TOFU fallback.
- Changed: `mngr imbue_cloud admin pool create` defaults `--backend` to `slice` (was `ovh_vps`). Existing OVH VPS pool hosts can still be listed and destroyed.
- Changed: `mngr imbue_cloud admin pool create --backend slice` now requires `--server-id` — baking always targets an explicitly-chosen, ready server rather than auto-selecting one.
- Changed: `mngr imbue_cloud admin pool create` validates `--region` against the known lease regions (`US-EAST-VA`, `US-WEST-OR`) and fails fast on anything else (most importantly a raw OVH datacenter code like `vin`, which `admin server list` prints). Previously a free-form region stamped onto `pool_hosts` made the host permanently unleasable.
- Changed: `mngr imbue_cloud admin pool list` (and the `minds pool list` wrapper) now emits every `pool_hosts` column (instead of a hand-maintained 10-column subset), so a baked slice no longer shows up looking like a region-less OVH VPS.
- Changed: `mngr imbue_cloud admin pool destroy` is now backend-aware: a `slice` row destroys its lima VM and data disk on the bare-metal box (freeing the slot) before the row is dropped; an `ovh_vps` row (including legacy rows written before the column existed) cancels its OVH VPS as before. Either teardown runs before the row delete, so a failure keeps the row and the operation stays retryable. Slice teardown reads the pool management key from `POOL_SSH_PRIVATE_KEY`. `--skip-vps-cancel` still drops the row only, for any backend.
- Changed: `mngr imbue_cloud admin server prep` now pre-installs the pinned Docker Engine and `inotify-tools` into the staged golden slice image via `virt-customize` (adds a `libguestfs-tools` box dependency), so slice carves no longer download/install Docker per VM — speeding up baking and removing a per-slice network dependency.
- Changed: Corrected bare-metal slice sizing so a box's slot count reflects what it can realistically run (also flows into `admin server pricing`). RAM overhead is now modeled in two parts (a per-machine host reserve + a per-VM overhead), so the guest gets its full advertised `memory_per_slice_gb` (previously it was silently shortchanged by the overhead). Disk no longer overcommits.
- Changed: `mngr list` discovery now reports a transport-level failure reaching the Imbue Cloud connector (connection refused, DNS failure, timeout) as a typed `ProviderUnavailableError` rather than a bare httpx error. Lets the minds recovery flow tell "the provider is unreachable, so a restart can't help — just retry" apart from "your workspace can't be reached for another reason".

### Deprecated

- Deprecated: Baking new OVH classic VPS pool hosts in `mngr imbue_cloud admin pool create`. Passing `--backend ovh_vps` fails with a deprecation error pointing at `--backend slice`. Existing OVH VPS pool hosts are unaffected and can still be listed and destroyed.

### Fixed

- Fixed: Restarting a stopped imbue_cloud (leased pool) mind no longer leaves it in a broken, unrecoverable state. `ImbueCloudProvider.get_host` now probes the inner container's running state over the outer root SSH (mirroring `VpsDockerProvider.get_host`) and returns an offline host when the container is stopped, so `mngr start` routes through `start_host`. `start_host` then relaunches the container's sshd over the outer root SSH (the in-container sshd is started via `docker exec` and does not survive a `docker stop`/`start`, but the container filesystem -- including the per-host authorized key and the served host key -- is preserved across the restart) and waits for it to accept connections.
- Fixed: `mngr create` on the imbue_cloud fast path (`fast_mode=require`) tolerates start args the pre-baked pool-host container already carries (`--security-opt=no-new-privileges`, `--workdir=/`, `--restart=unless-stopped`) instead of failing with "does not accept --image or --start-arg". Any other start arg, or an `--image` swap, still requires `fast_mode=prevent` to rebuild.
- Fixed: Host lock reporting for imbue_cloud pool hosts now derives status from a real flock held-probe rather than the lock file's presence.
- Fixed: Slice fast-path leases no longer hang at "Waiting for initial chat agent..." — the slice bake now stops the `system-services` agent post-bake so the lease starts it fresh.
- Fixed: Bare-metal box-prep bug that made every slice bake fail with `mkdir ~/.cache/lima: permission denied` — prep now creates and chowns the cache dir chain to the lima user (and repairs an already-root-owned `~/.cache` when re-run).

## [v0.1.6] - 2026-06-18

### Added

- Added: `mngr imbue_cloud admin server order --option <planCode>` (repeatable) lets you order plans whose mandatory option families (e.g. bandwidth, vrack) offer more than one choice. Previously the cart build failed with "expected exactly one X option to auto-pick" on such plans (e.g. the `24sys*` SYS line). Single-offer families are still auto-selected; an `order` run without `--option` on an ambiguous plan now lists each family's offers and their monthly prices so you can re-run with the right values.
- Added: `mngr imbue_cloud admin pool create --backend slice --max-concurrency N` (default 4) bounds slice-bake parallelism: bakes at most N at once and queues the rest, reporting progress as each completes. Keeps box contention low enough that each `mngr create` finishes within its per-create timeout (raised to 45 minutes for slices). After bakes finish, the slice backend reconciles lima VMs and **lima data disks** against the pool DB and reaps any orphan; the reap also runs on a top-level SIGTERM/SIGINT.

### Changed

- Changed: `mngr imbue_cloud admin pool destroy` is now backend-aware (mirroring the `--backend` branch in `pool create`). A `slice` row destroys its lima VM and data disk on the bare-metal box (freeing the slot) before the row is dropped; an `ovh_vps` row (including legacy rows written before the column existed) cancels its OVH VPS as before. Either teardown runs before the row delete, so a failure keeps the row and the operation stays retryable. `--skip-vps-cancel` still drops the row only, for any backend. Direct `admin pool destroy` of a slice requires `POOL_SSH_PRIVATE_KEY` (the `minds pool destroy` wrapper injects it from Vault).
- Changed: `mngr imbue_cloud admin server prep` now pre-installs the pinned Docker Engine and `inotify-tools` into the staged golden slice image via `virt-customize` (adds a `libguestfs-tools` box dependency). Slice carves no longer download/install Docker per VM, which speeds up baking (especially in parallel) and removes a per-slice network dependency. `server prep` also now provisions a 32 GiB swapfile.
- Changed: Corrected bare-metal slice sizing so a box's slot count reflects what it can realistically run (this also flows into `admin server pricing`). RAM overhead is now modeled in two parts (a per-machine host reserve and a per-VM overhead) so the guest gets its full advertised `memory_per_slice_gb`. Disk no longer overcommits: the reserve absorbs the GB-vs-GiB gap plus partition/filesystem overhead.
- Changed: `mngr imbue_cloud admin pool create --backend slice` now requires `--server-id` (the bare-metal box to bake slices onto, from `admin server list`). Baking always targets an explicitly-chosen, ready server rather than auto-selecting one.

### Removed

- Removed: Dead `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` stubs from `LimaSliceVpsClient`, matching the slimmed-down `VpsClientInterface`. No user-facing behavior change: these methods only ever raised "unavailable".

### Fixed

- Fixed: Bare-metal box-prep bug that made every slice bake fail with `mkdir ~/.cache/lima: permission denied`. The prep script (run as root) staged the slice base image under the lima user's `~/.cache` but left `~/.cache` itself root-owned, so `limactl` (run as the lima user) could not create `~/.cache/lima`. Prep now creates and chowns the cache dir chain to the lima user, and repairs an already-root-owned `~/.cache` when re-run.

## [v0.1.5] - 2026-06-16

### Changed

- Changed: `destroy_host` now raises a `CleanupFailedGroup` carrying the classified cleanup failures (instead of returning them, or swallowing errors as warnings) when a resource is left behind, and returns normally otherwise. A leased VPS that cannot be released back to the pool is recorded as a `HOST_RESOURCE_REMAINS` failure (the data-wipe step on the VPS stays best-effort / warn-only because the released VPS is destroyed wholesale by `cleanup_released_hosts.py`), so `mngr destroy`/`cleanup` can surface it and exit with a cause-specific code. See `specs/cleanup-error-aggregation.md`.

### Removed

- Removed: Dead `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` stub overrides from `LimaSliceVpsClient`, matching the removal of those abstract methods from the shared `VpsClientInterface`.

## [v0.1.4] - 2026-06-16

### Added

- Added: OVH bare-metal "slices" feature — carve VPS-like hosts (lima/QEMU VMs) out of rented OVH bare-metal servers as an alternative to ordering OVH VPSes. A slice is indistinguishable from a baked VPS pool host to minds and the imbue_cloud provider, with cleaner btrfs (the lima data disk, no loopback). Includes the slice data model + lifecycle (`bare_metal.py`), lima slice creation (`build_slice_lima_yaml`, `LimaSliceVpsClient`), and a `SliceVpsDockerProvider` that runs the shared vps_docker bake on the VM.
- Added: `mngr imbue_cloud admin server` command group for the bare-metal lifecycle — `pricing` (per-slice OVH pricing table), `order` (places a real OVH eco order; **charges the account**), `await-delivery`, `setup` (resumable Debian reinstall + box prep), `register`, `list`, `set-status`. Codifies the full RAM-pricing → order → deliver → provision → slice flow that was previously done by hand.
- Added: `mngr imbue_cloud admin pool create --backend [ovh_vps|slice]` unifies pool-host baking across backends — there is now a single command to bake a leasable pool host regardless of backend (the machine-provisioning step differs; the bake + row insert are shared). Slice rows go through the same lease-metadata path as OVH, carrying the operator's `--attributes` and `--region`.
- Added: `--skip-deferred-install-wait` flag on `admin pool create` (slice + ovh_vps) for faster dev/throwaway pool bakes that skip the FCT deferred-install (heavy apt + Playwright/Chromium) wait; must not be used for production pool hosts.

### Changed

- Changed: Pool bake now waits for the FCT `deferred-install` service to finish before stopping the services agent, on both the OVH-VPS and slice paths. Stopping mid-apt previously corrupted dpkg, leaving the deferred install failing on every post-lease retry until repaired.
- Changed: `admin pool create` no longer accepts hand-typed `repo_url` / `repo_branch_or_tag` in `--attributes`. The bake source is now exactly one of `--from-tag <tag>` (production — clones `--repo-url` at the tag into a fresh temp dir so the content provably equals the tag) or `--workspace-dir <dir>` (dev — bakes from a working tree). `--attributes` is now optional and rejects the `repo_url` / `repo_branch_or_tag` keys.
- Changed: imbue_cloud fast path now matches on the **repository** as well as the branch/tag, so it can no longer adopt a pool host running different code than the request asked for. A new `repo_identity.canonicalize_repo_source` is the single source of truth applied identically at bake time and request time (normalizes ssh/https, `.git`, trailing slash, host case; resolves a local path to its `origin` remote). `fast_mode=require` now raises `FastPathUnavailableError` when canonical identity cannot be established, instead of matching on a subset.
- Changed: Restructured the `mngr_imbue_cloud` plugin into layered sub-packages (`plugin`, `cli`, `bake`, `providers`, `hosts`, `slices`, `connector`) with an `import-linter` "mngr_imbue_cloud layers contract" enforcing the ordering. The slice/bare-metal subsystem is isolated in `slices/`, the provider-generic pool bake in `bake/`, and both provider backends are co-located in `plugin/backends.py`. Plugin entry points moved to `imbue.mngr_imbue_cloud.plugin.entrypoints` / `plugin.slice_entrypoints`. Pure refactor: no behavior, CLI, wire-format, or schema change.
- Changed: Decomposed the oversized `providers/instance.py` (~2,000 lines) — extracted the pure listing-shaping helpers into `providers/listing.py`, the pre-release data-wipe script generator into `providers/wipe.py`, and the slow-path VPS-vs-slice rebuild provider/config builders into `providers/rebuild.py` (with their unit tests co-located).
- Changed: The imbue_cloud provider now reaches a leased host's outer (VPS-root) sshd at the lease's `ssh_port` instead of a hardcoded 22, so `mngr list` / discovery and destroy-time wipe target the slice VM rather than the bare-metal box's own sshd.
- Changed: The slow-path rebuild now pins the leased host's outer SSH host key in the rebuilding provider's known_hosts, so the certified-data sync over the outer connection passes strict host-key checking (applies to OVH VPSes and slices).

## [v0.1.3] - 2026-06-15

## [v0.1.2] - 2026-06-13

### Changed

- Changed: A stopped (offline) host's files are now readable through the same interface as an online host (used e.g. by Claude session preservation when a host is destroyed while offline). The host's volume is resolved lazily on first read, so this adds no per-host probe to host discovery; when no volume is available, reads behave as "nothing there".
- Changed: `_build_delegated_vps_provider` now returns a `MinimalVpsDockerProvider` (moved into `mngr_vps_docker`, since it's a generally useful role for any externally-managed-VPS host-setup path). Its `_parse_build_args` extracts `--git-depth=N` and forwards everything else to docker, which is the correct behavior for the no-provisioning path that pairs with `ExternallyManagedVpsClient`; without this, every slow-path container rebuild raised before any docker work happened (the base `_parse_build_args` is `@abstractmethod` now).
- Changed: `mngr imbue_cloud admin pool create` now passes `--ovh-datacenter=` instead of the retired `--vps-datacenter=` to the inner `mngr create --provider ovh`, keeping pool creation working after the OVH provider's per-provider build-arg prefix rename.
- Changed: Replaced direct ValueError/RuntimeError raises in build-arg parsing and host provisioning with dedicated custom exception types.

## [v0.1.1] - 2026-06-08

### Added

- Added: `--no-recycle` flag on `mngr imbue_cloud admin pool create` that forces a fresh OVH VPS order (sets `MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED=false` on the inner `mngr create`) instead of reclaiming a cancelled (still-billable) VPS, for exercising the fresh-provision path.
- Added: Region-aware leasing — `mngr create` against imbue_cloud accepts a hard `-b region=<datacenter>` build arg (lease fails if no host is available in that datacenter), validated against the known OVH-US datacenters (`US-EAST-VA`, `US-WEST-OR`), and applied on both the fast and slow paths. `mngr imbue_cloud admin pool create` records the bake `--region` so the connector can filter on it.
- Added: Auto-discovered as a publishable package by the release tooling; will be offered for first publication to PyPI on the next release.

### Changed

- Changed: Rebuilt containers now run under the gVisor (`runsc`) runtime with `--workdir=/` and `no-new-privileges` hardening args, configured per account by minds bootstrap.
- Changed: The imbue_cloud slow (rebuild) path now re-applies the full idempotent host setup (pinned Docker version, gVisor `runsc` install/registration, sshd tuning, base packages) on the leased VPS before rebuilding the container, so a workspace created via the slow path — even on a host baked before runsc existed — comes up consistent and runs its agent container under gVisor. A failure is fatal.

### Removed

- Removed: The soft `-b preferred_region=<dc>` lease build arg. A lease is now constrained only by the hard `-b region=<dc>` arg; when unset, the lease is region-agnostic.

## [v0.1.0] - 2026-06-05

### Added

- Added: New `mngr imbue_cloud bucket` command group (`create` / `list` / `info` / `destroy`) for managing per-host R2 buckets (paid accounts only), plus `bucket keys create/list/destroy` for minting and revoking bucket-scoped S3 keys (read-only or read-write). `bucket create` returns S3-compatible credentials as JSON; the secret is shown only once and never stored. `bucket destroy` refuses a non-empty bucket and otherwise cascades to revoke its keys.
- Added: A pure helper exposing the rendered host-wipe shell script so it can be unit-tested without an SSH transport.
- Added: New `mngr imbue_cloud admin paid` subcommands for managing the connector's paid-user lists: `paid domain add|remove|list` and `paid email add|remove|list` (with `--paid-only` on list). These talk to the connector's `/paid/*` admin API using the fixed API key read from `$MINDS_PAID_ADMIN_KEY` (or `--api-key`). Matching client methods and a `PaidListEntry` data type are exposed.
- Added: Robust "slow path" for imbue_cloud host leasing, selected by a new `-b fast_mode=require|prevent` build arg. `require` adopts an exactly-matching pre-baked agent (the original fast path); `prevent` (the new default) leases any adequately-sized host and rebuilds its container from the FCT Dockerfile, releasing the lease if setup fails.

### Changed

- Changed: `mngr destroy <agent>` against an imbue_cloud-leased pool host is now terminal rather than a soft `docker stop`. The new flow stops + removes the workspace container, drops the per-host docker volume and btrfs subvolume, prunes the system, wipes `/root` + `/tmp` (preserving `/root/.ssh/authorized_keys`), releases the lease back to the pool, then cleans up local per-host state. Privacy-first ordering wipes data before flipping the row to `released`. `mngr delete <agent>` runs the same flow and is a safe no-op for an already-released lease. Use `mngr stop <agent>` instead to pause the container without releasing the lease.
- Changed: `mngr imbue_cloud admin pool destroy` (and the `minds pool destroy` wrapper) now do a full teardown: cancel the OVH VPS (strip per-lease tags + `deleteAtExpiration`) before dropping the row, so destruction can no longer strand a still-billing VPS. Pass `--skip-vps-cancel` only when the VPS is already gone. The provider's `destroy_host` now also raises when the connector release fails instead of silently cleaning up local state, so a failed release no longer makes mngr "forget" a host whose lease/VPS is still live.
- Changed: Stopped masking errors in the lease/teardown paths — host-listing and host-release failures now raise instead of being swallowed (the create-rollback path still catches release errors explicitly to stay best-effort).
- Changed: Bumped the `imbue-mngr` pin from `0.2.8` to `0.2.10` to align with main's release commit, so building the `apps/minds` ToDesktop bundle from main no longer fails at `uv lock`.
- Changed: Simplified an exception handler now that the host error types are all `MngrError` subclasses. No behavior change.
- Changed: `mngr imbue_cloud admin pool create` is now provider-generic — adds a required `--region REGION` and repeatable `--tag KEY=VALUE`, defaults to the OVH templates/provider, and installs + configures `ufw` on every leased VPS.
- Changed: A leased host now adopts the user-supplied host name (rewritten into the container) so the FCT bootstrap's initial chat uses the user's chosen name instead of the bake's placeholder.
- Changed: The bake's services agent now uses the constant name `system-services`, and the bake clears the FCT bootstrap's initial-chat state so the user's first start re-fires it cleanly.
- Changed: Agent lookup now filters by both agent name and host name, so an operator's local state accumulating one `system-services` agent per bake no longer routes calls to the wrong VPS.
- Changed: Offline plugin fields are now populated for leased hosts that fall back to offline/lease-only data.
- Changed: Added to the release tooling's publish graph; will be offered for first publication to PyPI on the next release. Previously-unpinned internal deps are now pinned, as a published wheel requires. No runtime change.

### Removed

- Removed: Dead env-injection helpers; the central `MINDS_API_KEY` is now injected on the fly by the latchkey gateway's `minds-api-proxy` extension and no longer needs to be pushed onto leased pool hosts.

### Fixed

- Fixed: `pool_hosts` INSERT now picks up the schema's `host_name` column; every successful pool bake had been dying at the last step with `null value in column "host_name"` and leaking a fully-provisioned VPS.
- Fixed: Multi-token `mngr exec` commands packed into a single `shlex.join`'d positional string so click no longer eats `--force` as a `mngr exec` option.
- Fixed: `mngr imbue_cloud auth oauth` no longer hangs until the 300s timeout after the browser already returned the OAuth code. The local callback listener now only records query params when the request is for `/oauth/callback` with non-empty params, so secondary GETs (favicon, prefetches) can no longer overwrite the captured callback with `{}`.
- Fixed: The slow (rebuild) path no longer trips on `python3: not found`. A rebuilt host was wrongly treated as carrying a pre-baked agent, so provisioning took the minimal "adopt" path; it now runs the standard full create + provision pipeline.
- Fixed: Pool-host bake no longer writes the wrong value into the VPS instance id column, which had broken every connector-side OVH teardown.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.

### Changed

- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
