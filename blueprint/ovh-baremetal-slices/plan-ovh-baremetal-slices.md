# OVH bare-metal slices for imbue_cloud

Extend the imbue_cloud pool with a second way to produce VPS-like hosts: instead of *ordering* an OVH classic VPS, we *carve* one ourselves by running a lima (QEMU) VM on a bare-metal OVH server we own. The slice ends up indistinguishable from a baked VPS pool host to minds and to `mngr_imbue_cloud`, but with cleaner btrfs handling. Motivated by the current lack of OVH VPS availability.

## Overview

- We build our own minimal "VPS service" on rented OVH bare metal: each "slice" is a lima VM (`is_run_as_root` + btrfs `additionalDisk`) that looks exactly like an OVH VPS to the rest of the stack.
- Slices reuse the **exact** OVH split of responsibilities: a new lima-backed `VpsClientInterface` (in `mngr_imbue_cloud`) handles only the "make/destroy the machine" step; the shared `VpsDockerProvider` does the docker container + FCT bake from the laptop, identical to OVH. The only divergence is the btrfs branch: the VM hands over an already-mounted btrfs disk, so vps_docker skips the loopback image.
- VPS-backed and slice-backed hosts live in **one shared pool**: a lease takes whichever `available` row matches first, fully transparent to minds and the `mngr_imbue_cloud` provider. The VPS-vs-slice distinction lives only in the connector's release path and in admin tooling, keyed off a new `backend_kind` discriminator.
- New admin surface in `mngr_imbue_cloud` covers the bare-metal lifecycle (order → register → install → reconcile → list → destroy) and slice allocation. Every step is independently runnable and resumable, because ordering/install can take a long time and partial failures are expected. The connector is not involved in provisioning, only in lease/release (mirroring today).
- Pricing is computed and shown correctly as a first-class part of the admin order flow (and gated behind an explicit confirm), so the true all-in cost is never misread — see the pricing gotcha below.
- First box (ordered): **`24rise02-v1-us`** / RISE-2 (Intel Xeon-E 2388G 8c/16t, 64 GB, 2×512 GB NVMe softraid → RAID1, `vin`/US-EAST-VA) → 8 × 8 GB slices. OVH order `8144904` placed 2026-06-13, month-to-month ($80 one-time setup + $93/mo).

## Expected behavior

- `minds` and the `mngr_imbue_cloud` provider behave identically whether a lease lands on a real OVH VPS or a slice — same lease/discover/destroy flow, same SSH shape (`vps_address` + `ssh_port` + `container_ssh_port`), same baked `system-services` agent. Minor leakage (e.g. backend type shown in admin views) is acceptable but not the goal.
- A slice presents as: `vps_address` = the bare-metal box's public address, `ssh_port` = a forwarded host port → the VM's root sshd, `container_ssh_port` = a second forwarded host port → the inner docker container's sshd. Each slice exposes exactly those two inbound ports.
- Slices are network-isolated from each other: lima's default per-VM user-mode/NAT means no VM-to-VM traffic, while every VM still has full outbound internet. No cross-VM ACLs to maintain.
- Slices are pre-baked into a warm `available` pool by an admin command (just like VPS pool hosts today); leasing stays a pure pick-an-available-row with no VM work at lease time.
- Releasing a lease on a slice destroys the lima VM entirely and frees its slot (slot becomes empty capacity to re-bake later); releasing a real VPS still cancels the OVH VPS as today. The connector does the slice teardown inline at release time.
- Slice data lives on an in-VM btrfs `additionalDisk` (no loopback image); the per-host-hex subvolume layout is preserved for parity with the OVH path.
- Each bare-metal box yields `floor(RAM_GB / 8)` slices; every slice advertises 8 GB RAM (allocated slightly under 8 GB so host + QEMU overhead fits) plus a `cpus` value derived from the box's cores with mild CPU overcommit (no RAM overcommit). For the first box that's 8 slices.
- Disk-failure robustness: the box's disks are mirrored (RAID1 on 2-disk boxes, RAID10 on 4+-disk), set at OS-install time, transparently protecting every slice's data.
- Operators can: order/register a box, drive its install to completion across multiple invocations, list all boxes with per-server total/used/free slot counts and fleet totals (from the DB, no fleet-wide `limactl`), reconcile a single box against `limactl` when needed, allocate new slices onto the box with the most free slots, and manually destroy a box (only when it has no leased slices; no `--force`, never automatic).
- No automatic failure recovery: if a box dies, its workspaces show `CRASHED` in `mngr list`; recovery is left to future application-level backup/migration.

## Changes

### Data model (connector `host_pool` Neon DB)

- New `bare_metal_servers` table tracking each rented box: OVH service name, region/datacenter, public address, detected specs (RAM/cores/disks), computed slot count, RAID layout, lima service-user, and a resumable lifecycle status (`ordered` → `delivered` → `installing` → `ready` → `failed`).
- Extend `pool_hosts` with: a `backend_kind` discriminator (`ovh_vps` vs `slice`), and for slices the owning bare-metal box reference plus the lima instance name and disk name needed to target the VM at teardown.
- New migration(s) following the existing numbered, idempotent convention; the admin CLI's `pool_hosts` INSERT and the new `bare_metal_servers` writes both go directly to the DB (laptop-side), mirroring today's pool-create. The connector only reads these tables (plus the release-time writes it already does).

### `mngr_imbue_cloud` — slice creation (new "our own VPS" path)

- Add a dependency on `mngr_lima`; add a lima-backed `VpsClientInterface` implementation whose `create_instance` SSHes to the box and uses the `mngr_lima` provider to bring up a VPS-parity VM (root SSH, btrfs `additionalDisk`, two `portForwards`), returning a VPS-shaped handle; whose `destroy_instance` tears the VM + disk down.
- Add a `VpsDockerProvider` subclass wired to that lima VpsClient, living inside `mngr_imbue_cloud`, so the shared container + FCT bake runs exactly as for OVH.
- Own the VPS-parity VM definition here (not an FCT template, not a generic `mngr create --template`): the lima config + port-forward + ufw setup that brings a VM to the same baseline a fresh OVH VPS provides.
- Teach the vps_docker container setup an "external btrfs already mounted, skip loopback" mode, selected when the VpsClient signals the disk is pre-provisioned; keep the per-host-hex subvolume structure.

### `mngr_imbue_cloud` — admin surface

- New `mngr imbue_cloud admin server` group: `order` (programmatic OVH dedicated-server cart→checkout for a chosen plan/datacenter), `register` (record an already-delivered box), `install` (OVH install API → Debian + RAID, then prep: install/sync mngr + lima + btrfs-progs, configure forwarding, create the lima service-user), `reconcile` (advance a non-`ready` box one step; run `limactl` on that box to heal slice state), `list` (servers + per-server/fleet slot accounting from the DB), `destroy` (manual-only OVH cancel; refuse if any slice leased; no `--force`).
- `order` (and a standalone `quote`/`price` subcommand) computes and prints the **true all-in price** before any checkout and requires an explicit operator confirm. It uses the eco order cart's authoritative price preview — build the cart (item + mandatory memory/storage/bandwidth/vrack options + datacenter/OS/region config), `assign` (non-committal), then `GET /order/cart/{id}/checkout` to read the exact one-time setup + recurring monthly + tax — rather than eyeballing the catalog "base" price.
- Shared pricing helper (used by both `order`/`quote` and `list`/fleet cost views) that parses a catalog plan into `{ base_monthly, per-addon monthly deltas, one-time setup, effective monthly per commitment }`, so cost is derived consistently everywhere instead of re-implemented per call site.
- Extend the existing `admin pool` group with slice allocation: pick the `ready` server with the most free slots, bake a slice via the new provider, and insert its `pool_hosts` row (`backend_kind=slice`, 8 GB + derived `cpus` attributes) — the slice analogue of today's `pool create`.
- Resumability throughout: order delivery and OS install are polled and recorded on the `bare_metal_servers` row so re-running a command continues a partially-provisioned box; the DB is source of truth fleet-wide, `limactl` is consulted only per-box during reconcile/allocate.
- Prep installs mngr on the box and rsyncs/vendors the operator's current monorepo onto it (mirroring today's vendor-sync) so the box runs the operator's mngr version.

### Connector (`remote_service_connector`) — release fork

- In `release_host`, branch on the `pool_hosts.backend_kind`: for `ovh_vps`, keep today's OVH cancel; for `slice`, SSH to the bare-metal box as the lima service-user (pool management key) and run `limactl delete` + drop the btrfs disk, then delete the row to free the slot.
- `lease_host` and `GET /hosts` are unchanged (slices are ordinary `pool_hosts` rows).

### Bare metal — first box (already ordered)

- **Ordered:** `24rise02-v1-us` (RISE-2, Intel Xeon-E 2388G 8c/16t, 64 GB → 8 × 8 GB slices), datacenter `vin` (US-EAST-VA), storage `softraid-2x512nvme` (2×512 GB NVMe, RAID1-capable), OS `none_64.en` (we install Debian 12 + RAID1 ourselves via the OVH install API), month-to-month (`P1M`).
- OVH order `8144904`, placed 2026-06-13 via the eco order cart (`/order/cart/{id}/eco`), auto-paid; $80 one-time setup + $93/mo. Delivery in progress at order time.
- Once delivered, this box is the target for the `admin server register` → `install` → slice-allocation flow during implementation.

### Pricing gotcha (must be encoded, not eyeballed)

- The catalog's per-plan "base" price is the price of the **minimum** configuration (smallest RAM, default storage). It does **not** include RAM/storage upgrades. True recurring = `base_monthly + sum(non-default selected addon monthly deltas)`.
- Concrete miss made during planning: RISE-2 was quoted at "$80/mo" by reading the base, but the mandatory 64 GB upgrade adds $13/mo, so the real recurring is **$93/mo**. The 64 GB upgrade's monthly delta was dropped from the comparison table.
- Eco-line orders also carry a **one-time setup fee ≈ one month** when taken month-to-month (it is waived on a 12/24-month commitment). This must be surfaced separately from the recurring price.
- Therefore: never quote from the catalog "base" alone. The admin `order`/`quote` path must always show `one-time setup + recurring monthly (+ effective monthly if committed) + tax`, sourced from the cart price preview, and require confirmation before checkout. The shared pricing helper is the single place this math lives.
