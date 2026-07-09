# Plan: Parallel pool-host destroy with lease-race protection

## Overview

- Pool upgrades ("bake new slices, destroy the old ones") currently require one `just destroy-pool-host <id>` invocation per slice, each run in sequence with its own Vault read, DB connection, and SSH session. Nothing fundamental forces this: only the carve-time reservation takes the box-wide lock, and `limactl delete` operates on per-instance state, so destroys can run in parallel — even on a single box.
- `mngr imbue_cloud admin pool destroy` (and the `minds pool destroy` wrapper) becomes variadic: one invocation takes N pool-host ids and destroys them all concurrently under a single global `--max-concurrency` bound (default 8), mirroring the bake's thread + semaphore pattern.
- The destroy-vs-lease race is closed with an atomic claim, reusing the connector's existing convention: commit `status='removing'` (via `UPDATE ... WHERE id=%s AND status IN (...)`) before any teardown. The connector's `/hosts/lease` only selects `status='available'` rows, so a claimed row can never be leased; a claim that updates 0 rows means the host was just leased, and it is skipped with a warning. Every destroy path funnels through this claim — including `--force` (which widens the claim to `leased` rows) and `--drop-row-only`.
- The stale `status='released'` guard is retired: nothing sets that status anymore (release deletes the row), so today every real destroy needs `--force`. The new default destroys `available` and stale `removing` rows without flags; `--force` is required only for `leased` rows.
- `teardown-slices` (the `minds env destroy` bulk path) gets the same claim and runs through the same parallel destroy helper, and now also includes rows stranded in `removing` by a crashed release, so an env destroy never leaks VMs or rows.

## Expected behavior

- `uv run minds pool destroy <id1> <id2> ... [--force] [--drop-row-only] [--max-concurrency N]` destroys all named pool hosts in one invocation: one Vault read, then all slice VMs torn down concurrently (at most `--max-concurrency` at once, default 8), regardless of which bare-metal box each slice lives on. The justfile recipe is renamed to the plural `destroy-pool-hosts` and takes variadic ids (clean break — no alias for the old recipe name).
- Each host is first atomically claimed by flipping its row to `status='removing'` in a committed transaction. Only after the claim does any SSH/limactl work happen, so a user lease attempt either wins the row before the claim (destroy skips it) or never sees it (lease matches only `available`).
- Without flags, rows in `available` or stale `removing` states are destroyed. A `leased` row is refused unless `--force` is passed; with `--force`, the claim's WHERE also covers `leased`, so the user's later release finds the row gone or unleased and returns idempotent `already_released`.
- The command keeps going through all ids on failure and ends with a JSON outcome report mirroring the bake's shape: `{requested, destroyed, skipped, failed, hosts: [{id, status, detail}]}` with per-host statuses like `destroyed` / `skipped_leased` / `already_gone` / `failed`. Exit code is non-zero only when a teardown actually failed; a claim-miss ("just got leased") is a warning, not a failure.
- Re-running the same id list after a partial failure converges: ids whose rows are already gone report `already_gone` (success), and rows left in `removing` by a failed teardown are re-claimed and retried.
- `--skip-vps-cancel` is removed and replaced by `--drop-row-only` (clean break, on both the admin CLI and the minds wrapper): drop the rows without attempting VM teardown, for rows whose box record is deleted or whose machine is permanently dead. It still goes through the atomic claim. When it is set, the minds wrapper skips the Vault key read (no SSH needed).
- `mngr imbue_cloud admin pool teardown-slices` claims each target row as `removing` before teardown, includes rows already stuck in `removing`, tears the VMs down through the same parallel helper, and emits the same unified outcome-report shape (its only consumer, `minds env destroy`, checks the exit code).
- DB-side slot accounting (`count_slices_on_server`) counts `removing` rows as still occupying their slot until the VM is actually gone, so `admin server list` capacity numbers stay truthful while destroys are in flight (bakes remain protected by the authoritative on-box occupancy check either way).
- The Cleanup section of `apps/minds/docs/host-pool-setup.md` documents the multi-id parallel destroy and the recommended pool-upgrade sequence (bake new slices at the new tag, then destroy the old `available` ids in one command).

## Changes

- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/admin.py`:
  - `pool destroy` takes variadic `pool_host_ids`, adds `--max-concurrency` (default 8), replaces `--skip-vps-cancel` with `--drop-row-only`, and re-scopes `--force` to mean "also destroy leased rows".
  - Per-id flow becomes: atomic claim (`removing`) → VM teardown (unless `--drop-row-only`) → row delete, with per-host outcomes aggregated into the bake-style JSON report and the exit code derived from teardown failures only.
  - `teardown-slices` routes through the same shared parallel-destroy helper and report shape.
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/slices/bare_metal_db.py`:
  - New claim function (single `UPDATE ... SET status='removing' WHERE id=%s AND status IN (...)` returning whether the row was claimed), with the eligible-status set widened by `--force`.
  - `_SELECT_UNLEASED_SLICE_TEARDOWN_TARGETS_SQL` stops excluding `removing` rows (keeps excluding `leased`).
  - `_COUNT_SLICES_SQL` counts `removing` rows as occupied.
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/server.py`:
  - Shared parallel teardown helper (threads + semaphore, modeled on `allocate_slices`) used by both `pool destroy` and `tear_down_unleased_slices`; one `LimaSliceVpsClient` per target, grouped lookups of box records up front.
  - `tear_down_unleased_slices` claims rows before teardown and returns the unified report.
- `apps/minds/imbue/minds/cli/pool.py`: `pool destroy` accepts multiple ids, forwards `--max-concurrency`, renames the skip flag to `--drop-row-only` (Vault key read skipped when set), and forwards everything to the admin command unchanged otherwise.
- `justfile`: rename `destroy-pool-host` → `destroy-pool-hosts` with variadic args; update the surrounding comments that mention the old name and flag.
- `apps/minds/docs/host-pool-setup.md`: update the Cleanup section (multi-id syntax, new flags, pool-upgrade workflow).
- Connector (`apps/remote_service_connector`): no code change — its lease (`FOR UPDATE SKIP LOCKED` on `available`) and release (`removing` claim) paths already compose correctly with the new admin-side claim.
- Tests: unit tests for the claim SQL builders/eligibility logic, outcome aggregation, and CLI arg wiring (admin + minds wrappers), following the existing pure-function test style (no real Postgres). Manual verification on `dev-josh-1`: bake 2-3 throwaway slices, destroy them in one parallel invocation, and drive a real `mngr create` lease attempt mid-destroy to observe the skip/claim behavior.
- Changelog entries: `libs/mngr_imbue_cloud/changelog/<branch>.md`, `apps/minds/changelog/<branch>.md`, `dev/changelog/<branch>.md`. Single PR.
