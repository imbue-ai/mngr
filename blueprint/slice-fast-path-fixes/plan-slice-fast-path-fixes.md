# Make OVH bare-metal slices bake + lease indistinguishably from OVH VPSes

Fix the divergences uncovered while trying to validate the fast-path FCT lease on a real
slice. The goal: from minds' and the forever-claude-template's perspective, a leased host
should behave identically whether it is a real OVH VPS or a lima-VM "slice" on one of our
bare-metal boxes — the *only* intended difference is the cleaner in-VM btrfs setup.

Companion docs: the design background is `blueprint/ovh-baremetal-slices/plan-ovh-baremetal-slices.md`
and `blueprint/ovh-baremetal-slices/HANDOFF.md`; the live validation runbook is
`blueprint/ovh-baremetal-slices/TEST-PLAN.md` (this plan supersedes that doc's "required
changes" list).

## Overview

- **Lease side is already shared; only narrow bugs remain.** Leasing always goes through the
  `imbue_cloud` provider + the existing `imbue_cloud` create-template regardless of backend,
  so the fixes are surgical, not a new parallel path.
- **D1 — outer SSH port bug.** The lease-side provider hardcodes the outer (VPS-root) SSH port
  to `22`. Correct for OVH (root sshd on :22), wrong for a slice (the VM-root sshd is a
  box-forwarded port). This makes `mngr list`/discovery and the destroy-time wipe hit the
  bare-metal box's own sshd instead of the slice VM, so a healthy slice shows as
  CRASHED/UNAUTHENTICATED. Fix: use the lease's `ssh_port` (OVH rows carry `22`, unchanged).
- **D2 — the slice bake isn't codified.** `admin server allocate-slice` is report-only. It
  must actually allocate: carve the VM + container + FCT bake on the box, authorize the pool
  management key, tear down the bootstrap chat agent, and insert the slice `pool_hosts` row —
  with the same parity contract the OVH pool bake already meets.
- **D3 — get this branch's mngr onto the box automatically.** The slice provider runs `limactl`
  locally on the box, so any box-touching admin command must first sync the operator's working
  tree to the box (once per run) instead of relying on ad-hoc SSH.
- **Reuse, don't fork.** Slices reuse the existing `ovh` FCT create-template (made
  provider-agnostic), the shared `vps_docker` bake/btrfs helpers (generalized to recognize an
  already-mounted btrfs), and `mngr create --format json` (extended once, for everyone) — so
  the slice and VPS paths stay one codebase, differing only at machine-carving and teardown.
- **Scope:** D1 + D2 + D3 + the slow-path btrfs generalization, covered by unit/integration
  tests plus one live run on the real box (`15.204.140.221`). The live-OVH server lifecycle
  (`order` / `install` / `reconcile`) stays out of scope until we deploy to staging.

## Expected behavior

- An operator runs a single command (`mngr imbue_cloud admin server allocate-slice`, env-aware)
  that picks the ready box with the most free slots, bakes one or more slices on it, and leaves
  `available` slice rows in the pool — the slice analogue of `admin pool create`.
  - `--count N` bakes N slices; the box-sync happens once and the N bakes run in parallel
    within the command (each slice probes for its own free ports, so concurrent bakes don't
    collide). Parallelizing at this level avoids the racing rsyncs that running the command N
    times would cause.
  - `--dry-run` reports the chosen placement + the attributes each slice would advertise,
    without baking (the old report-only behavior, now opt-in).
  - Partial failure bakes every slice it can, prints `{succeeded, failed, failures}` JSON, and
    exits non-zero if any failed (mirrors `pool create`).
- A baked slice is an ordinary `available` `pool_hosts` row (`backend_kind='slice'`, the two
  box-forwarded ports, `attributes={memory_gb:8, cpus:<derived>}`, the lima instance/disk
  names, owning `bare_metal_server_id`) — leasing picks it via the existing attribute match,
  transparent to minds.
- A user leases a slice on the fast path exactly as for a VPS
  (`mngr create <ws>@.imbue_cloud_<account> -b fast_mode=require -b memory_gb=8 -b cpus=<n>`),
  the connector injects the user's key on both forwarded ports, and the pre-baked
  `system-services` agent is adopted. `mngr exec`, `mngr connect`, and `mngr list` all report
  the slice correctly (no spurious CRASHED).
- The slow path (`fast_mode=prevent`) also works on a slice: the container is rebuilt in place
  on the VM, reusing the VM's already-mounted btrfs disk (no loopback image is created).
- Releasing a slice (`mngr destroy`) tears down the lima VM + data disk on the box via the
  connector's existing slice branch and frees the slot; releasing a VPS still cancels OVH.
- `mngr create --format json` returns the host's SSH connection details (address, user,
  container port, outer port, host name) for every provider, not just `{agent_id, host_id}`.

## Changes

### D1 — lease-side outer SSH port (`libs/mngr_imbue_cloud/.../instance.py`)
- `ImbueCloudProvider.outer_host_for` and `_ensure_outer_host_key_known` use the lease's
  `ssh_port` instead of the literal `22` when opening / key-scanning the outer (VPS-root) SSH.
- OVH rows store `ssh_port=22`, so their behavior is unchanged; slice rows carry their
  box-forwarded VM-root port, so discovery and the destroy-time wipe reach the actual VM.

### D4 — share the bake template (`forever-claude-template/.mngr/settings.toml`)
- Remove the redundant `provider = "ovh"` line from `[create_templates.ovh]` so the template is
  provider-agnostic; the provider comes from the create address suffix (the only caller already
  passes `@host.ovh`). The same template then bakes both `@host.ovh` and
  `@host.imbue_cloud_slice`. No new FCT create-template is added.
- (Provider config for the slice — `box_public_address`, `host_dir=/mngr` — is supplied by the
  `allocate-slice` command per-run from the `bare_metal_servers` row, not an FCT settings block.)

### Capture — extend `mngr create --format json` (`libs/mngr/.../cli/create.py`)
- The JSON/JSONL create result gains the host's SSH connection block: address, ssh user,
  container SSH port, the outer SSH port, and host name (in addition to `agent_id`/`host_id`).
- For most providers the outer port is the conventional value (e.g. 22); the slice provider
  reports its forwarded VM-root port. `lima_instance_name`/`lima_disk_name` are *not* added to
  the JSON — they are derived from `host_id` via the existing deterministic helpers.

### Slice provider — persist + report both ports (`libs/mngr_imbue_cloud/.../slice_provider.py`, `lima_slice_client.py`)
- The slice provider records both forwarded ports (VM-root and container) on its persisted host
  record so they survive past the in-memory bake and can surface through the create result.
- The slice provider's bespoke btrfs override collapses into the generalized shared helper
  (below), keeping only the genuinely slice-specific seams (per-host ports).

### Slow-path + bake btrfs — generalize the shared helper (`libs/mngr_vps_docker/.../container_setup.py`)
- `prepare_btrfs_on_outer` detects when `btrfs_mount_path` is already a mounted btrfs filesystem
  (the slice case) and, if so, skips loop-file allocation/mkfs/mount and just ensures the
  per-host subvolume.
- This makes both the base `VpsDockerProvider` bake and the slow path's delegated
  `MinimalVpsDockerProvider` rebuild handle slices transparently — no `backend_kind` is plumbed
  through the lease response, and the slice provider no longer needs its own btrfs override.

### D2 + D3 — `allocate-slice` actually allocates (`libs/mngr_imbue_cloud/.../cli/server.py`, supporting modules)
- `allocate-slice` becomes the slice analogue of `admin pool create`:
  - Resolve placement (ready box with the most free slots) from the pool DB (existing logic).
  - **Sync once (D3):** rsync the operator's live working tree to the box's lima service user
    (`limahost@<box>:~/mngr`) and `uv sync --all-packages`, idempotently — reusing the same
    exclude rules as the minds vendor-sync / `admin pool create` rsync. Codified into the
    command; no manual SSH.
  - **Bake (parallel for `--count N`):** for each slice, run the FCT bake on the box against the
    `imbue_cloud_slice` provider (shared `ovh` template, address-selected provider,
    `box_public_address` injected from the box row), capturing ids + both ports via
    `mngr create --format json`.
  - **Authorize the pool key:** derive the pool management public key from
    `POOL_SSH_PRIVATE_KEY` (reusing `server.py:_derive_public_key`) and install it on both the
    VM root and the inner container, so the connector's lease-time key injection succeeds.
  - **FCT bootstrap teardown:** destroy the bootstrap-created chat agent and remove the
    initial-chat sentinel, so the user's first lease re-creates the chat agent under their own
    workspace name (parity with `_create_single_pool_host`).
  - **Insert the row:** build + insert the slice `pool_hosts` row via the existing
    `build_slice_pool_host_insert_values` / `insert_slice_pool_host`, deriving the lima
    instance/disk names from `host_id`.
  - `--count N` (parallel internally), `--dry-run` (placement preview only), and
    `{succeeded, failed, failures}` accounting with non-zero exit on any failure.
- Connector ↔ box reachability (Modal must reach the box on :22 and the forwarded ports) is a
  documented operational precondition, not a code change.

### Docs
- Regenerate the imbue_cloud CLI docs (`libs/mngr/docs/commands/secondary/imbue_cloud.md`) for
  the new `allocate-slice` flags.
- Per-project changelog entries for each touched project (`mngr`, `mngr_imbue_cloud`,
  `mngr_vps_docker`, and `dev` if root files change) plus the FCT changelog entry.
