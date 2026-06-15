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
